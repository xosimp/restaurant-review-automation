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
from models import (init_db, get_conn, approve_response,
                    get_reviews_since, get_restaurant)
from auth import (init_auth, verify_password, create_session,
                  get_session_user, delete_session, create_user,
                  list_users, update_password)
from dotenv import load_dotenv
import pathlib
load_dotenv(pathlib.Path(__file__).parent / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(32).hex())

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "will")
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

def get_review_stats(restaurant_id):
    conn = get_conn()
    total   = conn.execute("SELECT COUNT(*) FROM reviews WHERE processed=1 AND restaurant_id=?", (restaurant_id,)).fetchone()[0]
    pos     = conn.execute("SELECT COUNT(*) FROM reviews WHERE sentiment='positive' AND restaurant_id=?", (restaurant_id,)).fetchone()[0]
    neg     = conn.execute("SELECT COUNT(*) FROM reviews WHERE sentiment='negative' AND restaurant_id=?", (restaurant_id,)).fetchone()[0]
    neu     = conn.execute("SELECT COUNT(*) FROM reviews WHERE sentiment='neutral' AND restaurant_id=?", (restaurant_id,)).fetchone()[0]
    urgent  = conn.execute("SELECT COUNT(*) FROM reviews WHERE urgency='high' AND response_status NOT IN ('posted','skipped') AND restaurant_id=?", (restaurant_id,)).fetchone()[0]
    avg_row = conn.execute("SELECT AVG(rating) FROM reviews WHERE processed=1 AND restaurant_id=?", (restaurant_id,)).fetchone()[0]
    drafted = conn.execute("SELECT COUNT(*) FROM reviews WHERE response_status='drafted' AND restaurant_id=?", (restaurant_id,)).fetchone()[0]
    conn.close()
    return dict(total=total, positive=pos, negative=neg, neutral=neu,
                urgent=urgent, avg_rating=round(avg_row or 0, 1),
                awaiting_approval=drafted)

def get_reviews_data(restaurant_id, filter_by="all", search=""):
    conn = get_conn()
    where = ["processed=1", f"restaurant_id={restaurant_id}"]
    if filter_by == "urgent":    where.append("urgency='high'")
    elif filter_by in ("positive","neutral","negative"): where.append(f"sentiment='{filter_by}'")
    elif filter_by == "pending": where.append("response_status='drafted'")
    if search:
        s = search.replace("'","''")
        where.append(f"(author LIKE '%{s}%' OR text LIKE '%{s}%')")
    rows = conn.execute(f"""SELECT * FROM reviews WHERE {' AND '.join(where)}
        ORDER BY CASE urgency WHEN 'high' THEN 0 ELSE 1 END,
        CASE sentiment WHEN 'negative' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END,
        fetched_at DESC""").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["categories"] = json.loads(d["categories"] or "[]")
        result.append(d)
    return result

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
<meta property="og:image" content="https://dashboard.cavnar.ai/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:site_name" content="Cavnar AI">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Cavnar AI — Restaurant Intelligence">
<meta name="twitter:description" content="AI-powered reviews, labor, inventory, and marketing for independent restaurants. Fully managed. No learning curve.">
<meta name="twitter:image" content="https://dashboard.cavnar.ai/og-image.png">
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
</style>
</head>
<body>
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
    <span class="hdr-user">{{ current_user.username }}</span>
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
    <button class="tab {{'active' if not mod_reviews and not mod_labor and not mod_inventory and not mod_marketing}}" onclick="switchTab('account',this)">Account</button>
  </nav>
</div>

<!-- REVIEWS -->
<div class="panel {{'active' if mod_reviews}}" id="panel-reviews">
  {% if not restaurant.reviews_live %}
  <div style="background:#fff8e6;border:1px solid #f0c040;border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:#8a6a00;display:flex;align-items:center;gap:8px">
    <span>⚠</span><span><strong>Sample data</strong> — example reviews to show how the dashboard works. Will is connecting your live Google and Yelp reviews.</span>
  </div>
  {% endif %}
  <div class="stat-row">
    <div class="stat"><div class="stat-n">{{rstats.avg_rating}}</div><div class="stat-l">Avg rating</div></div>
    <div class="stat"><div class="stat-n">{{rstats.total}}</div><div class="stat-l">Total</div></div>
    <div class="stat ok"><div class="stat-n">{{rstats.positive}}</div><div class="stat-l">Positive</div></div>
    <div class="stat warn"><div class="stat-n">{{rstats.neutral}}</div><div class="stat-l">Neutral</div></div>
    <div class="stat hi"><div class="stat-n">{{rstats.negative}}</div><div class="stat-l">Negative</div></div>
    <div class="stat hi"><div class="stat-n">{{rstats.urgent}}</div><div class="stat-l">Urgent</div></div>
    <div class="stat warn"><div class="stat-n">{{rstats.awaiting_approval}}</div><div class="stat-l">To approve</div></div>
  <div class="stat {{'ok' if restaurant.reviews_live else 'warn'}}">
    <div class="stat-n" style="font-size:14px;margin-top:4px">{{'Live' if restaurant.reviews_live else 'Demo'}}</div>
    <div class="stat-l">Review source</div>
  </div>
  </div>
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
  <div class="card {{'urgent' if r.urgency=='high'}} {{'approved' if r.response_status=='approved'}}" id="rc-{{r.id}}">
    {% if r.urgency=='high' %}<div class="ubanner">⚠ Needs immediate attention</div>{% endif %}
    <div class="card-hd">
      <div class="avatar" style="background:{{col}}">{{r.author[0].upper()}}</div>
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
            <span style="font-size:11px;color:var(--green);font-weight:500">✓ Posted</span>
          {% elif r.response_status=='approved' %}
            <span class="btn btn-approved">✓ Approved</span>
            <button class="btn btn-skip" onclick="skipR({{r.id}})">Edit</button>
            <button class="btn" style="background:#e8f0fe;color:#1a56cc;border:1px solid #c5d8f8;font-size:11px" onclick="markPosted({{r.id}})">Mark as posted</button>
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
  <div style="background:#fff8e6;border:1px solid #f0c040;border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:#8a6a00;display:flex;align-items:center;gap:8px">
    <span>⚠</span><span><strong>Sample data</strong> — example figures showing how the labor analysis works. Send your shift data to will@cavnar.ai to activate live data.</span>
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
  <div class="stat-row">
    <div class="stat hi"><div class="stat-n">${{inv.total_waste_cost_week|format_num}}</div><div class="stat-l">Waste/week</div></div>
    <div class="stat hi"><div class="stat-n">${{inv.monthly_waste_projection|int|format_num}}</div><div class="stat-l">Projected/mo</div></div>
    <div class="stat ok"><div class="stat-n">${{inv.recoverable_monthly|int|format_num}}</div><div class="stat-l">Recoverable</div></div>
    <div class="stat warn"><div class="stat-n">{{inv.waste_items|length}}</div><div class="stat-l">Waste items</div></div>
    <div class="stat hi"><div class="stat-n">{{inv.critical_low|length}}</div><div class="stat-l">Critical low</div></div>
    <div class="stat"><div class="stat-n">${{inv.total_stock_value|int|format_num}}</div><div class="stat-l">Inventory value</div></div>
  </div>
  {% if not inv.is_live %}
  <div style="background:#fff8e6;border:1px solid #f0c040;border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:#8a6a00;display:flex;align-items:center;gap:8px">
    <span>⚠</span>
    <span><strong>Sample data</strong> — this is example inventory data. Will will update this with your real numbers after your first weekly export.</span>
  </div>
  {% endif %}
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
    <div style="font-size:13px;font-weight:600;color:var(--ink)">
      Week of {{inv.week_start}} – {{inv.week_end}}
    </div>
    <div style="font-size:11px;color:var(--ink3)">
      Last updated: {{inv.last_updated}}
    </div>
  </div>
  <div class="insight"><div class="insight-lbl">Cavnar AI Food Cost Analysis</div><div class="insight-text insight-loading" id="inv-insight">Loading analysis…</div></div>
  <div style="background:#f0faf4;border:1px solid #a7d7b8;border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:#2d6a4f;line-height:1.6">
    <strong>How this works:</strong> Your inventory data is managed and updated by Will Cavnar weekly.
    To get the most accurate analysis, export your inventory or waste log from your POS system
    and send it to <a href="mailto:will@cavnar.ai" style="color:#2d6a4f;font-weight:600">will@cavnar.ai</a> each week.
    Will handles all the setup and updates — nothing for you to configure.
  </div>
  <div class="two-col">
    <div>
      <div class="slabel">Top waste offenders</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Item</th><th>Wasted</th><th>Cost</th><th>%</th></tr></thead>
        <tbody>{% for item in inv.waste_items %}<tr>
          <td><strong>{{item.item}}</strong></td><td>{{item.waste_last_week}} {{item.unit}}</td>
          <td><span class="pill pill-red">${{item.waste_cost}}</span></td><td>{{item.waste_pct}}%</td>
        </tr>{% else %}<tr><td colspan="4" style="color:#2d6a4f;font-style:italic;padding:12px;text-align:center">
          ✓ No significant waste flagged this week — great job.
        </td></tr>{% endfor %}</tbody></table></div>
      {% if inv.reorder_soon %}
      <div class="slabel" style="margin-top:12px">Order this week</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Item</th><th>Days left</th><th>Current stock</th></tr></thead>
        <tbody>{% for item in inv.reorder_soon %}<tr>
          <td><strong>{{item.item}}</strong></td>
          <td><span class="pill pill-amber">{{item.days_remaining}}d</span></td>
          <td>{{item.current_stock}} {{item.unit}}</td>
        </tr>{% endfor %}</tbody></table></div>
      {% endif %}
    </div>
    <div>
      <div class="slabel">Overstocked</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Item</th><th>Stock</th><th>Par</th><th>Excess</th></tr></thead>
        <tbody>{% for item in inv.overstock %}<tr>
          <td><strong>{{item.item}}</strong></td><td>{{item.current_stock}}</td>
          <td>{{item.par_level}}</td><td><span class="pill pill-amber">${{item.overstock_cost}}</span></td>
        </tr>{% else %}<tr><td colspan="4" style="color:#2d6a4f;font-style:italic;padding:12px;text-align:center">✓ Nothing overstocked this week.</td></tr>{% endfor %}</tbody></table></div>
      {% if inv.critical_low %}
      <div class="slabel" style="margin-top:12px">Critical low — order today</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Item</th><th>Days left</th><th>Action</th></tr></thead>
        <tbody>{% for item in inv.critical_low %}<tr>
          <td><strong>{{item.item}}</strong></td><td>{{item.days_remaining}}d</td>
          <td><span class="pill pill-red">Order now</span></td>
        </tr>{% endfor %}</tbody></table></div>
      {% endif %}
    </div>
  </div>
</div>

<!-- MARKETING -->
<div class="panel {{'active' if not mod_reviews and not mod_labor and not mod_inventory and mod_marketing}}" id="panel-marketing">
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
    <input class="topic-input" id="mktopic" placeholder="Topic or occasion — e.g. new spring menu, Mother's Day brunch…" value="New spring menu launch">
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
    <div class="cal-grid" id="cal-grid"><div class="no-data" style="grid-column:1/-1;padding:20px">Click "Generate week" for content ideas.</div></div>
  </div>

  <!-- Instagram connect banner -->
  <div id="ig-connect-banner" style="margin-top:14px;background:linear-gradient(135deg,#1a1410,#2a1f1a);border:1px solid #3a2a20;border-radius:var(--r);padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px">
    <div>
      <div style="font-size:13px;font-weight:600;color:var(--paper);margin-bottom:3px">Connect Instagram &amp; Facebook</div>
      <div style="font-size:12px;color:#7a736a;line-height:1.5">Connect your Instagram Business account and/or Facebook Business Page to post directly from the dashboard — no copy/paste needed.</div>
    </div>
    <a href="/instagram/connect" style="flex-shrink:0;background:var(--ember);color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:600;white-space:nowrap">Connect →</a>
  </div>
  <div id="ig-connected-banner" style="margin-top:14px;background:#eaf4ee;border:1px solid #b7dfca;border-radius:var(--r);padding:12px 16px;display:none;align-items:center;justify-content:space-between;gap:12px">
    <div>
      <div style="font-size:13px;color:#2d6a4f;font-weight:500">✓ Instagram &amp; Facebook connected</div>
      <div style="font-size:11px;color:#2d6a4f;margin-top:2px">Generate content then post to Instagram, Facebook, or both</div>
    </div>
    <button onclick="disconnectInstagram()" style="font-size:11px;color:#7a736a;background:transparent;border:none;cursor:pointer;text-decoration:underline">Disconnect</button>
  </div>
</div>

<!-- ACCOUNT -->
<div class="panel {{'active' if not mod_reviews and not mod_labor and not mod_inventory and not mod_marketing}}" id="panel-account">

  <!-- Your consultant -->
  <div class="slabel">Your consultant</div>
  <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:20px;display:flex;align-items:center;gap:16px">
    <div style="width:48px;height:48px;border-radius:50%;background:var(--ink);display:flex;align-items:center;justify-content:center;flex-shrink:0">
      <span style="font-family:'DM Serif Display',serif;font-size:18px;color:var(--ember);font-style:italic">W</span>
    </div>
    <div style="flex:1">
      <div style="font-weight:600;font-size:14px;margin-bottom:2px">Will Cavnar</div>
      <div style="font-size:12px;color:var(--ink3);margin-bottom:8px">Founder, Cavnar AI — manages your dashboard, data, and setup</div>
      <a href="mailto:will@cavnar.ai" style="font-size:12px;color:var(--ember);text-decoration:none;font-weight:600">will@cavnar.ai</a>
      <span style="color:var(--paper3);margin:0 8px">·</span>
      <span style="font-size:12px;color:var(--ink3)">Same-day response</span>
    </div>
  </div>

  <div class="two-col" style="margin-bottom:0">
    <div>

      <!-- Account overview -->
      <div class="slabel">Your account</div>
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:16px">
        <table style="font-size:13px;width:100%">
          <tr><td style="color:var(--ink3);padding:5px 0;width:140px">Restaurant</td><td style="font-weight:500">{{restaurant.name}}</td></tr>
          <tr><td style="color:var(--ink3);padding:5px 0">Email</td><td>{{restaurant.owner_email}}</td></tr>
          <tr><td style="color:var(--ink3);padding:5px 0">Username</td><td>{{current_user.username}}</td></tr>
        </table>
      </div>

      <!-- What's included -->
      <div class="slabel">What's included</div>
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:16px">
        {% if mod_reviews %}
        <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--paper3)">
          <span style="font-size:16px;flex-shrink:0">⭐</span>
          <div>
            <div style="font-size:13px;font-weight:600;color:var(--ink)">Review Intelligence</div>
            <div style="font-size:11px;color:var(--ink3);line-height:1.5;margin-top:2px">AI drafts responses to your Google and Yelp reviews — you approve before anything goes out</div>
          </div>
          <span style="margin-left:auto;font-size:10px;font-weight:600;color:var(--green);background:var(--green-bg);padding:2px 8px;border-radius:20px;flex-shrink:0;align-self:center">Active</span>
        </div>
        {% endif %}
        {% if mod_labor %}
        <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--paper3)">
          <span style="font-size:16px;flex-shrink:0">📊</span>
          <div>
            <div style="font-size:13px;font-weight:600;color:var(--ink)">Labor Optimizer</div>
            <div style="font-size:11px;color:var(--ink3);line-height:1.5;margin-top:2px">Weekly analysis of your labor cost vs target with an AI-optimized schedule to download</div>
          </div>
          <span style="margin-left:auto;font-size:10px;font-weight:600;color:var(--green);background:var(--green-bg);padding:2px 8px;border-radius:20px;flex-shrink:0;align-self:center">Active</span>
        </div>
        {% endif %}
        {% if mod_inventory %}
        <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--paper3)">
          <span style="font-size:16px;flex-shrink:0">📦</span>
          <div>
            <div style="font-size:13px;font-weight:600;color:var(--ink)">Inventory Control</div>
            <div style="font-size:11px;color:var(--ink3);line-height:1.5;margin-top:2px">Weekly food cost analysis — waste offenders, overstock, and what to order before you run out</div>
          </div>
          <span style="margin-left:auto;font-size:10px;font-weight:600;color:var(--green);background:var(--green-bg);padding:2px 8px;border-radius:20px;flex-shrink:0;align-self:center">Active</span>
        </div>
        {% endif %}
        {% if mod_marketing %}
        <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0">
          <span style="font-size:16px;flex-shrink:0">📣</span>
          <div>
            <div style="font-size:13px;font-weight:600;color:var(--ink)">Marketing Autopilot</div>
            <div style="font-size:11px;color:var(--ink3);line-height:1.5;margin-top:2px">AI-written social posts, emails, and promotions in your restaurant's voice — copy and post in seconds</div>
          </div>
          <span style="margin-left:auto;font-size:10px;font-weight:600;color:var(--green);background:var(--green-bg);padding:2px 8px;border-radius:20px;flex-shrink:0;align-self:center">Active</span>
        </div>
        {% endif %}
        {% if not mod_reviews and not mod_labor and not mod_inventory and not mod_marketing %}
        <div style="font-size:13px;color:var(--ink3);padding:8px 0">No modules active. Contact Will to get set up.</div>
        {% endif %}
      </div>

      <!-- Onboarding status -->
      <div class="slabel">Setup status</div>
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:16px">
        {% if mod_reviews %}
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;font-size:13px">
          {% if restaurant.reviews_live %}
          <span style="color:var(--green);font-size:14px">✓</span>
          <span>Reviews connected — pulling live data from Google & Yelp</span>
          {% else %}
          <span style="color:var(--amber);font-size:14px">○</span>
          <span style="color:var(--ink2)">Reviews — Will is connecting your live data</span>
          {% endif %}
        </div>
        {% endif %}
        {% if mod_labor %}
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;font-size:13px;border-top:1px solid var(--paper3)">
          {% if labor.is_live %}
          <span style="color:var(--green);font-size:14px">✓</span>
          <span>Labor data connected</span>
          {% else %}
          <span style="color:var(--amber);font-size:14px">○</span>
          <span style="color:var(--ink2)">Labor — send your shift export to <a href="mailto:will@cavnar.ai" style="color:var(--ember)">will@cavnar.ai</a> to activate</span>
          {% endif %}
        </div>
        {% endif %}
        {% if mod_inventory %}
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;font-size:13px;border-top:1px solid var(--paper3)">
          {% if inv.is_live %}
          <span style="color:var(--green);font-size:14px">✓</span>
          <span>Inventory data connected</span>
          {% else %}
          <span style="color:var(--amber);font-size:14px">○</span>
          <span style="color:var(--ink2)">Inventory — send your weekly export to <a href="mailto:will@cavnar.ai" style="color:var(--ember)">will@cavnar.ai</a> to activate</span>
          {% endif %}
        </div>
        {% endif %}
        {% if mod_marketing %}
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;font-size:13px;border-top:1px solid var(--paper3)">
          <span style="color:var(--green);font-size:14px">✓</span>
          <span>Marketing — ready to use now</span>
        </div>
        {% endif %}
      </div>

      <!-- Change password -->
      <div class="slabel">Change password</div>
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:16px">
        <div class="form-group"><label class="form-label">Current password</label><input class="form-input" type="password" id="pw-current" placeholder="••••••••"></div>
        <div class="form-group"><label class="form-label">New password</label><input class="form-input" type="password" id="pw-new" placeholder="min 8 characters"></div>
        <div class="form-group"><label class="form-label">Confirm new password</label><input class="form-input" type="password" id="pw-confirm" placeholder="••••••••"></div>
        <button class="btn-primary" onclick="changePassword()">Update password</button>
        <div id="pw-status" style="font-size:12px;margin-top:8px;display:none"></div>
      </div>
    </div>

    <div>
      <!-- Billing — next charge always visible -->
      <div class="slabel">Billing</div>
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:16px" id="billing-card">
        <div id="billing-loading" style="font-size:13px;color:var(--ink3)">Loading billing info…</div>
        <div id="billing-content" style="display:none">
          <div id="billing-next-banner" style="background:var(--paper2);border-radius:6px;padding:10px 14px;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between">
            <div>
              <div style="font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em;font-weight:600;margin-bottom:2px">Next charge</div>
              <div style="font-size:16px;font-weight:600;color:var(--ink)" id="billing-next-prominent">—</div>
            </div>
            <div style="text-align:right">
              <div style="font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em;font-weight:600;margin-bottom:2px">Amount</div>
              <div style="font-size:16px;font-weight:600;color:var(--ink)" id="billing-amount-prominent">—</div>
            </div>
          </div>
          <table style="font-size:13px;width:100%;margin-bottom:12px">
            <tr><td style="color:var(--ink3);padding:4px 0;width:120px">Status</td>
                <td id="billing-status" style="font-weight:500"></td></tr>
            <tr><td style="color:var(--ink3);padding:4px 0">Payment</td>
                <td id="billing-pm" style="font-weight:500"></td></tr>
          </table>
          <a id="billing-portal-link" href="#" target="_blank"
             style="display:inline-block;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;background:var(--paper2);border:1px solid var(--paper3);color:var(--ink2)">
            Manage payment method →
          </a>
        </div>
        <div id="billing-no-sub" style="display:none;font-size:13px;color:var(--ink3)">
          No active subscription found. <a href="mailto:will@cavnar.ai" style="color:var(--ember)">Contact Will</a> for help.
        </div>
      </div>

      <!-- Weekly digest -->
      {% if mod_reviews %}
      <div class="slabel">Weekly digest email</div>
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:16px">
        <p style="font-size:13px;color:var(--ink2);line-height:1.6;margin-bottom:12px">
          Your weekly review summary is emailed every
          <strong id="digest-day-current">{{restaurant.digest_day|title}}</strong> at 9am.
        </p>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select id="digest-day-select" style="padding:7px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px">
            {% for d in ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"] %}
            <option value="{{d}}" {{"selected" if restaurant.digest_day==d}}>{{d|title}}</option>
            {% endfor %}
          </select>
          <select id="digest-enabled-select" style="padding:7px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px">
            <option value="1" {{"selected" if restaurant.digest_enabled}}>Enabled</option>
            <option value="0" {{"selected" if not restaurant.digest_enabled}}>Disabled</option>
          </select>
          <button onclick="saveDigestDay()"
            style="padding:7px 16px;background:var(--ember);color:white;border:none;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;cursor:pointer">
            Save
          </button>
          <span id="digest-save-status" style="font-size:12px;display:none"></span>
        </div>
      </div>
      {% endif %}

      <!-- Support -->
      <div class="slabel">Support</div>
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:16px">
        <p style="font-size:13px;color:var(--ink2);line-height:1.6;margin-bottom:12px">
          Questions, data requests, or anything not working — reach out directly.
        </p>
        <a href="mailto:will@cavnar.ai"
           style="display:inline-block;background:var(--ember);color:white;padding:9px 18px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">
          Email Will →
        </a>
      </div>

      <!-- Cancellation — always visible -->
      <div class="slabel">Cancel subscription</div>
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
        <p style="font-size:13px;color:var(--ink2);line-height:1.6;margin-bottom:12px">
          No cancellation fees. Cancel before your next billing date to avoid the next charge.
        </p>
        <a href="mailto:will@cavnar.ai?subject=Cancel%20my%20Cavnar%20AI%20subscription&body=Hi%20Will%2C%20I%20would%20like%20to%20cancel%20my%20Cavnar%20AI%20subscription%20for%20{{restaurant.name}}."
           style="display:inline-block;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:500;border:1px solid var(--paper3);color:var(--ink3)">
          Request cancellation
        </a>
      </div>
    </div>
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
async function regenDraft(id) {
  const box = document.getElementById('draft-box-'+id);
  const txtEl = document.getElementById('draft-txt-'+id);
  const editorEl = document.getElementById('editor-text-'+id);
  if (txtEl) txtEl.textContent = 'Generating new response…';
  const res = await fetch('/api/regenerate-draft/'+id, {method:'POST'});
  const data = await res.json();
  if (data.ok) {
    if (txtEl) txtEl.textContent = data.draft;
    if (editorEl) editorEl.value = data.draft;
    document.getElementById('draft-actions-'+id).innerHTML =
      `<button class="btn btn-approve" onclick="approveR(${id})">✓ Approve</button>
       <button class="btn btn-skip" onclick="skipR(${id})">Skip</button>`;
    document.getElementById('draft-actions-'+id).style.display='flex';
    document.getElementById('editor-'+id).style.display='none';
    toast('New draft generated');
  } else {
    if (txtEl) txtEl.textContent = 'Error generating — try again';
    toast('Error: ' + (data.error||'unknown'));
  }
}
async function saveDraft(id) {
  const editorEl = document.getElementById('editor-text-'+id);
  const draft = editorEl ? editorEl.value.trim() : '';
  if (!draft) { toast('Response cannot be empty'); return; }
  const save = await fetch('/api/save-draft/'+id, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({draft})
  });
  const sd = await save.json();
  if (!sd.ok) { toast('Save failed'); return; }
  // Then approve
  const approve = await fetch('/approve/'+id, {method:'POST'});
  const ad = await approve.json();
  if (ad.ok) {
    const txtEl = document.getElementById('draft-txt-'+id);
    if (txtEl) txtEl.textContent = draft;
    document.getElementById('editor-'+id).style.display='none';
    document.getElementById('draft-actions-'+id).innerHTML =
      '<span class="btn btn-approved">✓ Approved</span>';
    document.getElementById('draft-actions-'+id).style.display='flex';
    toast('Response saved and approved');
  }
}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600)}
function switchTab(n,btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+n).classList.add('active');btn.classList.add('active');
  if(n==='labor'&&!laborLoaded){loadLaborInsight();}
  if(n==='marketing'){checkInstagramStatus();}
  if(n==='inventory'&&!invLoaded)loadInvInsight();
  if(n==='labor'){renderBars(); loadLaborTrend();}
  if(n==='account')loadBillingInfo();
  fetch('/api/log-activity',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tab:n})});
}
// Auto-load data for whichever tab is active on page load
window.addEventListener('DOMContentLoaded', function() {
  // Handle Instagram OAuth result
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('ig_connected') === '1') {
    toast('Instagram connected successfully ✓');
    checkInstagramStatus();
    history.replaceState({}, '', '/');
  }
  if (urlParams.get('ig_error')) {
    toast('Instagram connection failed: ' + urlParams.get('ig_error'));
    history.replaceState({}, '', '/');
  }
  const activePanel = document.querySelector('.panel.active');
  if(!activePanel) return;
  const id = activePanel.id.replace('panel-','');
  if(id==='labor'&&!laborLoaded){
    loadLaborInsight();
    setTimeout(renderBars, 100);
    setTimeout(loadLaborTrend, 200);
  }
  if(id==='inventory'&&!invLoaded){loadInvInsight();}
  if(id==='account'){loadBillingInfo();}
});
let rfilter='{{rfilter}}';
function setRF(f,btn){rfilter=f;document.querySelectorAll('.fpill').forEach(p=>p.classList.remove('active','active-red'));btn.classList.add(f==='urgent'?'active-red':'active');filterReviews()}
function filterReviews(){const q=document.getElementById('rsearch').value;window.location='/?filter='+rfilter+'&search='+encodeURIComponent(q)}
function approveR(id){fetch('/approve/'+id,{method:'POST'}).then(r=>r.json()).then(d=>{if(d.ok){document.getElementById('rc-'+id).classList.add('approved');document.querySelector('#rc-'+id+' .draft-actions').innerHTML='<span class="btn btn-approved">✓ Approved</span>';toast('Response approved')}})}
function skipR(id){
  fetch('/skip/'+id,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){
      const actions = document.getElementById('draft-actions-'+id);
      if(actions) {
        actions.innerHTML = `
          <button class="btn btn-approve" onclick="approveR(${id})">✓ Approve</button>
          <button class="btn btn-skip" onclick="openEditor(${id})">Edit response</button>
          <button class="btn btn-skip" onclick="regenDraft(${id})">↻ Regenerate</button>`;
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
let laborLoaded=false,invLoaded=false;
function loadLaborInsight(){
  laborLoaded=true;
  fetch('/api/labor-insight').then(r=>r.json()).then(d=>{
    const elLaborInsight=document.getElementById('labor-insight');
    elLaborInsight.textContent=d.insight||'Analysis unavailable — check back shortly.';
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
  fetch('/api/inv-insight').then(r=>r.json()).then(d=>{
    const elInvInsight=document.getElementById('inv-insight');
    elInvInsight.textContent=d.insight||'Analysis unavailable — check back shortly.';
    elInvInsight.classList.remove('insight-loading');
  }).catch(e=>{
    const elInvErr=document.getElementById('inv-insight');
    elInvErr.textContent='Analysis unavailable — check back shortly.';
    elInvErr.classList.remove('insight-loading');
  });
}
let selCt='{{ctypes[0].id if ctypes}}';
function selectCt(id,el){selCt=id;document.querySelectorAll('.ct-btn').forEach(b=>b.classList.remove('selected'));el.classList.add('selected')}
function genContent(){
  const topic=document.getElementById('mktopic').value.trim();
  if(!topic){toast('Enter a topic');return;}
  const box=document.getElementById('mkoutput');
  box.style.fontStyle='italic';
  box.style.color='var(--ink3)';
  box.textContent='Generating…';
  fetch('/api/generate-content',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:selCt,topic})})
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
function loadCal(){const g=document.getElementById('cal-grid');g.innerHTML='<div class="no-data" style="grid-column:1/-1;padding:16px">Generating…</div>';fetch('/api/content-calendar').then(r=>r.json()).then(d=>{if(!d.ideas||!d.ideas.length){g.innerHTML='<div class="no-data" style="grid-column:1/-1">Could not generate.</div>';return}const calDownBtn=document.getElementById('cal-download-btn');
  if(calDownBtn) calDownBtn.style.display='inline-block';
  g.innerHTML=d.ideas.map((i,idx)=>{
    window._calIdeas=window._calIdeas||[];
    window._calIdeas[idx]=i;
    return `<div class="cal-card"><div class="cal-day-name">${i.day}</div><div class="cal-platform" style="font-size:10px;color:var(--ink3);margin:2px 0 4px">${i.platform||''}</div><div style="font-size:12px;line-height:1.5">${i.angle||''}</div><button data-idx="${idx}" onclick="generateFromCalIdx(this.dataset.idx)" style="margin-top:8px;padding:4px 10px;font-size:10px;font-weight:600;background:var(--ember);color:white;border:none;border-radius:4px;cursor:pointer;font-family:'DM Sans',sans-serif;width:100%">Generate →</button></div>`;
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
  genContent();
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
    if(d.portal_url) document.getElementById('billing-portal-link').href=d.portal_url;
    else document.getElementById('billing-portal-link').style.display='none';
  }).catch(()=>{document.getElementById('billing-loading').textContent='Billing info unavailable.';});
}
// Instagram connect/post
async function checkInstagramStatus() {
  const res = await fetch('/api/instagram-status');
  const data = await res.json();
  const connectBanner   = document.getElementById('ig-connect-banner');
  const connectedBanner = document.getElementById('ig-connected-banner');
  const postBtn         = document.getElementById('ig-post-btn');
  const fbBtn = document.getElementById('fb-post-btn');
  if (data.connected) {
    if (connectBanner)   connectBanner.style.display   = 'none';
    if (connectedBanner) connectedBanner.style.display = 'flex';
    if (postBtn)         postBtn.style.display         = 'inline-flex';
    if (fbBtn)           fbBtn.style.display           = data.fb_connected ? 'inline-flex' : 'none';
  } else {
    if (connectBanner)   connectBanner.style.display   = 'flex';
    if (connectedBanner) connectedBanner.style.display = 'none';
    if (postBtn)         postBtn.style.display         = 'none';
    if (fbBtn)           fbBtn.style.display           = 'none';
  }
}
async function postToFacebook() {
  const caption = document.getElementById('mkoutput').textContent.trim();
  if (!caption || caption === 'Select a type and click Generate.') {
    toast('Generate content first'); return;
  }
  const btn = document.getElementById('fb-post-btn');
  btn.textContent = 'Posting…'; btn.disabled = true;
  const res = await fetch('/api/post-to-facebook', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({caption})
  });
  const data = await res.json();
  if (data.ok) {
    toast('Posted to Facebook ✓');
    btn.textContent = '✓ Posted';
    setTimeout(() => { btn.textContent = 'Post to Facebook ↗'; btn.disabled = false; }, 3000);
  } else {
    toast('Post failed: ' + (data.error || 'unknown error'));
    btn.textContent = 'Post to Facebook ↗'; btn.disabled = false;
  }
}
async function postToInstagram() {
  const caption = document.getElementById('mkoutput').textContent.trim();
  if (!caption || caption === 'Select a type and click Generate.') {
    toast('Generate content first'); return;
  }
  const btn = document.getElementById('ig-post-btn');
  btn.textContent = 'Posting…'; btn.disabled = true;
  const res = await fetch('/api/post-to-instagram', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({caption})
  });
  const data = await res.json();
  if (data.ok) {
    toast('Posted to Instagram ✓');
    btn.textContent = '✓ Posted';
    setTimeout(() => { btn.textContent = 'Post to Instagram ↗'; btn.disabled = false; }, 3000);
  } else {
    toast('Post failed: ' + (data.error || 'unknown error'));
    btn.textContent = 'Post to Instagram ↗'; btn.disabled = false;
  }
}
async function disconnectInstagram() {
  await fetch('/api/instagram-disconnect', {method:'POST'});
  checkInstagramStatus();
  toast('Instagram disconnected');
}
// Check IG status when marketing tab is opened
const _origSwitchTab = switchTab;
window.addEventListener('DOMContentLoaded', function() {
  // Check if already on marketing tab
  if (document.getElementById('panel-marketing')?.classList.contains('active')) {
    checkInstagramStatus();
  }
});

// Mark as posted
async function markPosted(id) {
  const res = await fetch('/api/mark-posted/'+id, {method:'POST'});
  const data = await res.json();
  if (data.ok) {
    const actions = document.getElementById('draft-actions-'+id);
    if (actions) actions.innerHTML = '<span style="font-size:11px;color:var(--green);font-weight:500">✓ Posted</span>';
    document.getElementById('rc-'+id)?.classList.remove('approved');
    toast('Marked as posted ✓');
  }
}

// Export reviews as CSV
function exportReviews() {
  window.location = '/api/export-reviews';
}

// Download content calendar as CSV
function downloadCal() {
  const ideas = window._calIdeas;
  if (!ideas || !ideas.length) { toast('Generate the calendar first'); return; }
  const rows = [['Day','Platform','Angle','Type']];
  ideas.forEach(i => rows.push([i.day||'', i.platform||'', i.angle||'', i.type||'']));
  const csv = rows.map(r => r.map(v => '"'+String(v).replace(/"/g,'""')+'"').join(',')).join('\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
  a.download = 'content_calendar.csv';
  a.click();
}

// Labor trend chart
async function loadLaborTrend() {
  const res = await fetch('/api/labor-trend');
  const data = await res.json();
  if (!data.weeks || !data.weeks.length) return;
  const container = document.getElementById('labor-trend-bars');
  const labels    = document.getElementById('labor-trend-labels');
  if (!container) return;
  const maxPct = Math.max(...data.weeks.map(w => w.pct), 35);
  const minPct = Math.max(0, Math.min(...data.weeks.map(w => w.pct)) - 5);
  const range = maxPct - minPct || 1;
  const maxH = 72;
  container.innerHTML = data.weeks.map(w => {
    const h = Math.max(6, Math.round(((w.pct - minPct) / range) * maxH));
    const col = w.pct > 32 ? 'var(--red)' : w.pct >= 28 ? '#ef9f27' : '#6fcf97';
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:2px">
      <span style="font-size:10px;color:${col};font-weight:600">${w.pct}%</span>
      <div style="width:80%;height:${h}px;background:${col};border-radius:3px 3px 0 0"></div>
    </div>`;
  }).join('');
  if (labels) labels.innerHTML = data.weeks.map(w =>
    `<span style="flex:1;text-align:center">${w.label}</span>`
  ).join('');
}

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
          <div class="status-dot {{'dot-live' if restaurant.reviews_live else 'dot-sample'}}"></div>
          <div class="status-text">
            {{'Pulling live reviews from Google/Yelp' if restaurant.reviews_live else 'Using sample review data — add Place ID and enable to go live'}}
          </div>
          <button class="toggle {{'on' if restaurant.reviews_live}}" id="reviews-live-toggle"
                  onclick="toggleReviewsLive(this)" title="Toggle live reviews"></button>
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

  <!-- Marketing profile -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Marketing profile</div></div>
    <div class="section-body">
      <div class="form-grid">
        <div class="form-group">
          <label>Neighborhood</label>
          <input type="text" id="neighborhood" value="{{ restaurant.neighborhood or '' }}" placeholder="Lincoln Park, Chicago">
        </div>
        <div class="form-group">
          <label>Known for</label>
          <input type="text" id="known_for" value="{{ restaurant.known_for or '' }}" placeholder="short rib pasta, brunch, craft cocktails">
          <div class="hint">Comma-separated list of signature items</div>
        </div>
        <div class="form-group full">
          <label>Restaurant vibe</label>
          <input type="text" id="vibe" value="{{ restaurant.vibe or '' }}" placeholder="warm neighborhood bistro, serious about food without being precious">
          <div class="hint">How you'd describe the restaurant's personality in one sentence</div>
        </div>
        <div class="form-group full">
          <label>Brand voice / tone notes</label>
          <textarea id="voice_notes" placeholder="e.g. Warm and genuine, a little witty, never corporate. Always invite guests back. Never sound like a PR firm.">{{ restaurant.voice_notes or '' }}</textarea>
          <div class="hint">Claude uses this to write review responses and marketing content in the owner's voice</div>
        </div>
        <div class="form-group full">
          <label>Never use these words or phrases</label>
          <input type="text" id="never_say" value="{{ restaurant.never_say or '' }}" placeholder="e.g. culinary journey, indulge, we strive to, it is our goal">
          <div class="hint">Comma-separated — Claude will avoid these in all generated content</div>
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
          <label>Sign-off name</label>
          <input type="text" id="sign_off_name" value="{{ restaurant.sign_off_name or '' }}" placeholder="e.g. Sarah, or The Maple Team">
          <div class="hint">Used at the end of emails and responses</div>
        </div>
        <div class="form-group">
          <label>Never say (words/phrases to avoid)</label>
          <input type="text" id="never_say" value="{{ restaurant.never_say or '' }}" placeholder="e.g. culinary journey, indulge, delightful">
          <div class="hint">Comma-separated — AI will never use these</div>
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

  <div class="save-bar">
    <button class="btn-save" onclick="saveSettings()">Save all settings</button>
    <a href="/admin/client-data/{{ restaurant.id }}" class="btn-data">Manage data →</a>
    <span class="save-status" id="save-status"></span>
  </div>
</div>

<script>
let reviewsLive = {{ 'true' if restaurant.reviews_live else 'false' }};


function toggleReviewsLive(btn) {
  reviewsLive = !reviewsLive;
  btn.classList.toggle('on', reviewsLive);
  btn.previousElementSibling.className = 'status-dot ' + (reviewsLive ? 'dot-live' : 'dot-sample');
  btn.previousElementSibling.nextElementSibling.textContent = reviewsLive
    ? 'Pulling live reviews from Google/Yelp'
    : 'Using sample review data — add Place ID and enable to go live';
}



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
    reviews_live:    reviewsLive,
    neighborhood:    document.getElementById('neighborhood').value,
    known_for:       document.getElementById('known_for').value,
    vibe:            document.getElementById('vibe').value,
    voice_notes:     document.getElementById('voice_notes').value,
    never_say:       document.getElementById('never_say').value,
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

  let csvContent = '';
  if (source === 'upload') {
    csvContent = fileData[module];
    if (!csvContent) { showResult(resultEl, false, 'No file selected'); return; }
  } else {
    csvContent = document.getElementById(module + '-paste-content').value;
    if (!csvContent.trim()) { showResult(resultEl, false, 'No data entered'); return; }
  }

  const form = new FormData();
  form.append('data_type', dataType);
  form.append('source', source);
  form.append('csv_content', csvContent);

  const res = await fetch('/admin/upload-data/' + restaurantId, {method:'POST', body: form});
  const data = await res.json();
  if (data.ok) {
    showResult(resultEl, true, '✓ ' + data.rows + ' rows saved successfully. Refresh to see updated status.');
    setTimeout(() => location.reload(), 2000);
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
      <div style="font-family:'DM Serif Display',serif;font-size:28px;color:var(--paper)">{{users|selectattr('is_active')|list|length}}</div>
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
      <div style="font-family:'DM Serif Display',serif;font-size:28px;color:var(--ember)">{{users|selectattr('contract_status','equalto','signed')|list|length}} / {{users|selectattr('is_active')|list|length}}</div>
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
  <div class="card" style="padding:0;overflow:visible">
    <table class="tbl">
      <thead><tr><th>Restaurant</th><th>Username</th><th>Email</th><th>Phone</th><th>Billing</th><th>Last login</th><th>Last tab</th><th>Last fetched</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>
      {% for user in users %}
      <tr class="client-row" data-group="{{user.location_group or ''}}">
        <td>
          <div style="display:flex;align-items:center;gap:6px">
            <strong>{{user.restaurant_name}}</strong>
            {% if user.internal_notes %}
            <span title="{{user.internal_notes}}" style="cursor:help;font-size:10px;background:var(--amber-bg);color:var(--amber);padding:1px 5px;border-radius:10px;font-weight:500">note</span>
            {% endif %}
          </div>
          {% if user.location_group %}
          <div style="font-size:10px;color:var(--ink3);margin-top:1px">
            {{user.location_group}}{% if user.location_name %} · {{user.location_name}}{% endif %}
          </div>
          {% endif %}
        </td>
        <td><code style="font-size:12px">{{user.username}}</code></td>
        <td>{{user.email}}</td>
        <td style="font-size:12px;color:var(--ink3)">{{user.phone or '—'}}</td>
        <td>
          {% set bc = {'trial':'#b7791f','active':'#2d6a4f','paused':'#6b7280','churned':'#c0392b'} %}
          <span style="font-size:10px;font-weight:500;padding:2px 8px;border-radius:20px;background:{% if user.billing_status == 'active' %}var(--green-bg){% elif user.billing_status == 'trial' %}var(--amber-bg){% else %}#f3f4f6{% endif %};color:{{ bc.get(user.billing_status,'#6b7280') }}">
            {{(user.billing_status or 'trial')|title}}
          </span>
          {% if not user.is_admin %}
          <div style="margin-top:3px">
            {% if user.contract_status == 'signed' %}
            <span style="font-size:9px;color:#2d6a4f;font-weight:600">✓ Signed</span>
            {% elif user.contract_status == 'sent' %}
            <span style="font-size:9px;color:#b7791f;font-weight:600">⏳ Awaiting signature</span>
            {% else %}
            <span style="font-size:9px;color:#9ca3af">No contract</span>
            {% endif %}
          </div>
          {% endif %}
        </td>
        <td style="font-size:12px">{% if user.last_login %}{% set d=user.last_login[:10].split('-') %}{{d[1]|int}}/{{d[2]|int}}/{{d[0][2:]}}{% else %}—{% endif %}</td>
        <td style="font-size:11px;color:var(--ink3)">{{user.last_active_tab or '—'}}</td>
        <td style="font-size:11px;color:var(--ink3)">{{user.last_fetched_at or 'never'}}</td>
        <td>
          {% if user.is_active %}
            <span class="badge-active">Active</span>
          {% else %}
            <span style="background:#f3f4f6;color:#6b7280;font-size:10px;padding:2px 7px;border-radius:20px;font-weight:500">Inactive</span>
          {% endif %}
        </td>
      <td>
        {% if not user.is_admin %}
        <div class="action-menu" id="menu-wrap-{{user.id}}">
          <button class="action-menu-btn" onclick="toggleMenu({{user.id}})">Actions ▾</button>
          <div class="action-dropdown" id="menu-{{user.id}}">
            <button class="action-item" onclick="window.location='/admin/client-settings/{{user.restaurant_id}}'">Settings</button>
            <button class="action-item" onclick="window.location='/admin/client-data/{{user.restaurant_id}}'">Manage data</button>
            <button class="action-item" onclick="window.location='/admin/view-as/{{user.restaurant_id}}'">View as client</button>
            {% if user.is_active %}
            <div class="action-divider"></div>
            <button class="action-item" onclick="resendPayment({{user.restaurant_id}},'{{user.email}}','{{user.billing_status}}');closeMenu({{user.id}})">Resend payment link</button>
            <button class="action-item" onclick="resendContract({{user.restaurant_id}});closeMenu({{user.id}})">Resend contract</button>
            <button class="action-item" onclick="seedReviews({{user.restaurant_id}});closeMenu({{user.id}})">Seed sample reviews</button>
            <div class="action-divider"></div>
            <button class="action-item action-item-danger" onclick="deactivateClient({{user.id}},'{{user.restaurant_name}}');closeMenu({{user.id}})">Deactivate</button>
            {% else %}
            <div class="action-divider"></div>
            <button class="action-item action-item-success" onclick="reactivateClient({{user.id}},'{{user.restaurant_name}}');closeMenu({{user.id}})">Reactivate</button>
            {% endif %}
          </div>
        </div>
        {% else %}—{% endif %}
      </td>
      </tr>
      {% else %}
      <tr><td colspan="7" style="color:var(--ink3);font-style:italic;padding:16px">No clients yet — create one above.</td></tr>
      {% endfor %}
      </tbody>
    </table>
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
  const res = await fetch('/admin/create-client', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  status.style.display = 'block';
  if (data.ok) {
    status.className = 'status-msg status-ok';
    let msg = `✓ Client created — username: ${payload.username}`;
    if (payload.send_email) msg += `. Welcome email sent to ${payload.owner_email}`;
    if (payload.service_tier && payload.service_tier !== "trial") msg += `. Payment links sent.`;
    status.textContent = msg;
    setTimeout(() => location.reload(), 1500);
  } else {
    status.className = 'status-msg status-err';
    status.textContent = data.error || 'Something went wrong';
  }
  btn.textContent = 'Create client account'; btn.disabled = false;
  status.style.display = 'block';
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
  <div style="max-width:900px;margin:0 auto">
  <div style="display:flex;align-items:center;justify-content:space-between;margin:24px 0 8px">
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
          {% set d=log.sent_at[:10].split('-') %}{{d[1]|int}}/{{d[2]|int}}/{{d[0][2:]}}
          <span style="color:var(--paper3)"> · </span>{{log.sent_at[11:16]}}
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


def send_payment_email(to_email, restaurant_name, tier=None,
                       module_count: int = None):
    """Send payment email with a dynamically generated Stripe checkout link."""
    if not RESEND_API_KEY:
        return

    # Determine module count
    if module_count is None:
        tier_counts = {
            "starter_reviews": 1, "starter_labor": 1,
            "starter_inventory": 1, "starter_marketing": 1,
            "full": 4,
        }
        module_count = tier_counts.get(tier, 0)

    if module_count == 0:
        return  # Trial — no payment needed

    setup_price    = f"${module_count * 500:,}"
    retainer_price = f"${module_count * 300:,}/mo"
    label = (
        "1 Module" if module_count == 1 else
        "Full System — 4 Modules" if module_count == 4 else
        f"{module_count} Modules"
    )

    # Generate dynamic Stripe checkout links — both monthly and annual
    checkout_monthly = create_stripe_checkout(module_count, to_email, restaurant_name, "monthly")
    checkout_annual  = create_stripe_checkout(module_count, to_email, restaurant_name, "annual")

    annual_price    = f"${module_count * 3000:,}/yr"
    annual_monthly  = f"${module_count * 250:,}/mo"

    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY
        if checkout_monthly and checkout_annual:
            btn_html = f"""
<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:4px">
  <div style="flex:1;min-width:200px;background:white;border:2px solid #c84b2f;border-radius:8px;padding:16px">
    <div style="font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin-bottom:4px">Monthly</div>
    <div style="font-size:20px;font-weight:600;color:#0e0c0a;font-family:Georgia,serif;margin-bottom:2px">{retainer_price}</div>
    <div style="font-size:11px;color:#7a736a;margin-bottom:12px">Cancel anytime</div>
    <a href="{checkout_monthly}" style="display:block;text-align:center;background:#c84b2f;color:white;padding:10px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Choose monthly →</a>
  </div>
  <div style="flex:1;min-width:200px;background:#fdf8f6;border:2px solid #2d6a4f;border-radius:8px;padding:16px;position:relative">
    <div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:#2d6a4f;color:white;font-size:10px;font-weight:600;padding:3px 10px;border-radius:20px;white-space:nowrap">2 MONTHS FREE</div>
    <div style="font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin-bottom:4px">Annual</div>
    <div style="font-size:20px;font-weight:600;color:#0e0c0a;font-family:Georgia,serif;margin-bottom:2px">{annual_price}</div>
    <div style="font-size:11px;color:#2d6a4f;font-weight:500;margin-bottom:12px">{annual_monthly}/mo — save ${{module_count*600:,}}</div>
    <a href="{checkout_annual}" style="display:block;text-align:center;background:#2d6a4f;color:white;padding:10px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Choose annual →</a>
  </div>
</div>"""
        elif checkout_monthly:
            btn_html = f'<a href="{checkout_monthly}" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:.04em">Complete payment →</a>'
        else:
            btn_html = '<p style="font-size:13px;color:#3a3530;margin-top:8px">Will will send your payment link shortly.</p>' 
        _resend.Emails.send({
            "from": f"Will Cavnar <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"Your Cavnar AI payment link — {restaurant_name}",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Restaurant Intelligence Dashboard
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6;margin-bottom:8px">
    Hi — excited to get started with <strong>{restaurant_name}</strong>.
    Here is your payment link for the <strong>{label}</strong> plan.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.6;margin-bottom:20px">
    Pick your plan below — {setup_price} setup is the same either way.
    Monthly at {retainer_price}, or save ${module_count*600:,} by going annual.
    30-day free trial on both — no charge until day 31.
  </p>
  <div style="background:#f7f4ef;border-radius:8px;padding:20px 22px;margin-bottom:24px;border-left:3px solid #c84b2f">
    <p style="font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin:0 0 6px">{label}</p>
    <div style="display:flex;gap:20px;margin-bottom:14px;flex-wrap:wrap">
      <div>
        <p style="font-size:18px;font-weight:600;color:#0e0c0a;margin:0;font-family:Georgia,serif">{setup_price}</p>
        <p style="font-size:11px;color:#7a736a;margin:0">today</p>
      </div>
      <div style="color:#e0dbd0;font-size:20px;line-height:1.8">+</div>
      <div>
        <p style="font-size:18px;font-weight:600;color:#0e0c0a;margin:0;font-family:Georgia,serif">{retainer_price}</p>
        <p style="font-size:11px;color:#7a736a;margin:0">starting day 31</p>
      </div>
    </div>
    {btn_html}
  </div>
  <p style="font-size:13px;color:#7a736a;line-height:1.6;margin-bottom:24px">
    I'll have your dashboard live within 24 hours of payment clearing.
    Any questions, just reply here.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Will Cavnar &nbsp;·&nbsp; Cavnar AI<br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
  </p>
</div>"""
        })
    except Exception as e:
        print(f"Payment email failed: {e}")

def _log_payment_email(to_email, restaurant_name, module_count):
    try:
        from models import log_email as _log_email, get_conn as _get_conn
        conn = _get_conn()
        row = conn.execute("SELECT id FROM restaurants WHERE owner_email=? LIMIT 1", (to_email,)).fetchone()
        conn.close()
        if row: _log_email(row[0], "payment", to_email, f"Your Cavnar AI payment link — {restaurant_name}")
    except Exception: pass

def send_welcome_email(to_email, restaurant_name, username, password,
                       module_reviews=0, module_labor=0,
                       module_inventory=0, module_marketing=0):
    """Send branded welcome email to new client with their login credentials."""
    import resend as _resend
    _resend.api_key = RESEND_API_KEY
    # Build module list
    active_modules = []
    if module_reviews:  active_modules.append("Review Intelligence")
    if module_labor:    active_modules.append("Labor Optimizer")
    if module_inventory: active_modules.append("Inventory Control")
    if module_marketing: active_modules.append("Marketing Autopilot")
    if not active_modules:
        active_modules = ["Review Intelligence"]  # fallback
    modules_count = len(active_modules)
    if modules_count == 1:
        modules_text = f"one module — {active_modules[0]}"
    else:
        modules_text = f"{modules_count} modules — " + ", ".join(active_modules[:-1]) + f", and {active_modules[-1]}"
    html = f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Restaurant Intelligence Dashboard
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6;margin-bottom:16px">
    Hi — your Cavnar AI dashboard for <strong>{restaurant_name}</strong> is live and ready to use.
  </p>
  <div style="background:#f7f4ef;border-radius:8px;padding:16px 20px;margin-bottom:20px">
    <p style="font-size:13px;color:#7a736a;margin:0 0 10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Your login details</p>
    <p style="font-size:14px;margin:0 0 6px"><strong>URL:</strong> <a href="https://dashboard.cavnar.ai" style="color:#c84b2f">dashboard.cavnar.ai</a></p>
    <p style="font-size:14px;margin:0 0 6px"><strong>Username:</strong> {username}</p>
    <p style="font-size:14px;margin:0"><strong>Temporary password:</strong> {password}</p>
  </div>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:12px">
    Once you log in, go to the <strong>Account</strong> tab to set your own password.
    Your dashboard includes {modules_text}, all set up specifically for {restaurant_name}.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:24px">
    Any questions, just reply to this email. I check it daily.
  </p>
  <p style="font-size:13px;color:#7a736a;line-height:1.6;margin-bottom:24px;padding:10px 14px;background:#f7f4ef;border-radius:6px;border-left:3px solid #c84b2f">
    <strong style="color:#3a3530">Note:</strong> This email may land in your Promotions tab. If it did, drag it to your Primary inbox — that way you won't miss any updates from me going forward.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Will Cavnar &nbsp;·&nbsp; Cavnar AI<br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
  </p>
</div>"""
    _resend.Emails.send({
        "from": f"Will Cavnar <{FROM_EMAIL}>",
        "to": [to_email],
        "subject": f"Your Cavnar AI dashboard is live — {restaurant_name}",
        "html": html,
    })

# ── Routes ────────────────────────────────────────────────────────────────────

@app.template_filter("format_num")
def format_num(v):
    try: return f"{float(v):,.0f}"
    except: return v

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        user = verify_password(username, password)
        if not user:
            return render_template_string(LOGIN_HTML, error="Invalid username or password")
        token = create_session(user["id"])
        next_url = request.args.get("next", "/admin" if user["is_admin"] else "/")
        resp = make_response(redirect(next_url))
        resp.set_cookie("session_token", token, max_age=30*24*3600,
                        httponly=True, samesite="Lax")
        return resp
    return render_template_string(LOGIN_HTML, error=None)

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
    rstats  = get_review_stats(rid)
    reviews = get_reviews_data(rid, rfilter, rsearch)
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
    except Exception as e:
        print(f"Inventory analysis error: {e}")
        inv = {"total_waste_cost_week":0,"monthly_waste_projection":0,
               "recoverable_monthly":0,"total_stock_value":0,
               "waste_items":[],"overstock":[],"critical_low":[],
               "reorder_soon":[],"total_items":0,
               "week_start":"—","week_end":"—","last_updated":"—",
               "is_live":False}
    # Show welcome banner if user has never logged in before (last_login is None)
    from auth import get_user_by_id
    _user_row = get_user_by_id(current_user["id"]) if not current_user.get("is_admin") else None
    show_welcome = bool(_user_row and not _user_row.get("last_login"))
    return render_template_string(DASHBOARD_HTML,
        show_welcome=show_welcome,
        current_user=current_user, restaurant=restaurant,
        rstats=rstats, reviews=reviews, rfilter=rfilter, rsearch=rsearch,
        labor=labor, inv=inv, ctypes=CONTENT_TYPES,
        mod_reviews=int(restaurant.module_reviews or 0),
        mod_labor=int(restaurant.module_labor or 0),
        mod_inventory=int(restaurant.module_inventory or 0),
        mod_marketing=int(restaurant.module_marketing or 0),
        now=datetime.now().strftime("%b %d, %Y"),
        viewing_as=current_user.get("is_admin", 0),
        labor_target=float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0)

@app.route("/approve/<int:rid>", methods=["POST"])
@login_required
def approve(rid, current_user):
    approve_response(rid)
    return jsonify(ok=True)

@app.route("/skip/<int:rid>", methods=["POST"])
@login_required
def skip(rid, current_user):
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='skipped' WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

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
        insight = get_claude_insights(analysis, restaurant_name=name, owner_name=owner)
        return jsonify(insight=insight)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(insight=f"Unable to load analysis. Error: {str(e)[:100]}")

@app.route("/api/inv-insight")
@login_required
def inv_insight_api(current_user):
    from inventory import load_inventory_for_restaurant, analyse_inventory, get_claude_insights
    restaurant = get_restaurant(current_user["restaurant_id"])
    items, _is_live = load_inventory_for_restaurant(current_user["restaurant_id"])
    analysis = analyse_inventory(items)
    owner_name = restaurant.owner_name if restaurant else None
    insight  = get_claude_insights(analysis, owner_name=owner_name, restaurant_name=restaurant.name if restaurant else None)
    return jsonify(insight=insight)

@app.route("/api/generate-content", methods=["POST"])
@login_required
def gen_content(current_user):
    data = request.get_json()
    from marketing import generate_content
    user = get_current_user()
    return jsonify(content=generate_content(
        data.get("type","instagram_post"), data.get("topic",""),
        restaurant_id=user["restaurant_id"] if user else None))

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

@app.route("/admin")
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
        enriched.append(u)
    from models import get_all_location_groups
    location_groups = get_all_location_groups()
    # Calculate MRR from active clients
    mrr = 0
    for u in enriched:
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

    return render_template_string(ADMIN_HTML,
        current_user=current_user, users=enriched,
        location_groups=location_groups,
        mrr=mrr,
        email_log=email_log)

@app.route("/admin/create-client", methods=["POST"])
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
            except Exception as e:
                print(f"DocuSign contract failed: {e}")
                import traceback; traceback.print_exc()

        # Steps 2 & 3 (payment + welcome emails) fire automatically
        # when the client signs the contract via the DocuSign webhook

        return jsonify(ok=True, restaurant_id=rid, envelope_id=envelope_id)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/admin/deactivate-client/<int:user_id>", methods=["POST"])
@admin_required
def deactivate_client(user_id, current_user):
    conn = get_conn()
    conn.execute("UPDATE users SET is_active=0 WHERE id=? AND is_admin=0", (user_id,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@app.route("/admin/reactivate-client/<int:user_id>", methods=["POST"])
@admin_required
def reactivate_client(user_id, current_user):
    conn = get_conn()
    conn.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@app.route("/admin/client-data/<int:restaurant_id>")
@admin_required
def client_data_page(restaurant_id, current_user):
    from models import get_client_data, get_staff_notes
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    data        = get_client_data(restaurant_id) or {}
    staff_notes = get_staff_notes(restaurant_id)
    return render_template_string(CLIENT_DATA_HTML,
        current_user=current_user,
        restaurant=restaurant,
        data=data,
        staff_notes=staff_notes)

@app.route("/admin/staff-notes/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_staff_note_route(restaurant_id, current_user):
    from models import save_staff_note
    name  = request.form.get("employee_name","").strip()
    notes = request.form.get("notes","").strip()
    if not name or not notes:
        return jsonify(ok=False, error="Name and notes required")
    save_staff_note(restaurant_id, name, notes)
    return jsonify(ok=True)

@app.route("/admin/staff-notes/<int:note_id>/delete", methods=["POST"])
@admin_required
def delete_staff_note_route(note_id, current_user):
    from models import delete_staff_note
    delete_staff_note(note_id)
    return jsonify(ok=True)

@app.route("/admin/upload-data/<int:restaurant_id>", methods=["POST"])
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

@app.route("/admin/client-settings/<int:restaurant_id>")
@admin_required
def client_settings_page(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    from models import get_client_data
    client_data = get_client_data(restaurant_id) or {}
    from models import get_staff_notes
    staff_notes = get_staff_notes(restaurant_id)
    return render_template_string(CLIENT_SETTINGS_HTML,
        current_user=current_user,
        restaurant=restaurant,
        client_data=client_data,
        staff_notes=staff_notes)

@app.route("/admin/client-settings/<int:restaurant_id>", methods=["POST"])
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
            "voice_notes":     data.get("voice_notes","").strip() or None,
            "neighborhood":    data.get("neighborhood","").strip() or None,
            "vibe":            data.get("vibe","").strip() or None,
            "known_for":       data.get("known_for","").strip() or None,
            "sign_off_name":   data.get("sign_off_name","").strip() or None,
            "never_say":       data.get("never_say","").strip() or None,
            "hourly_rate":     float(data.get("hourly_rate") or 26.0),
            "labor_target_pct": float(data.get("labor_target_pct") or 30.0),
            "pos_system":      data.get("pos_system","").strip() or None,
            "module_reviews":  int(data.get("module_reviews", 1)),
            "module_labor":    int(data.get("module_labor", 0)),
            "module_inventory":int(data.get("module_inventory", 0)),
            "module_marketing":int(data.get("module_marketing", 0)),
            "owner_name":      data.get("owner_name","").strip() or None,
            "owner_phone":     data.get("owner_phone","").strip() or None,
            "location_group":        data.get("location_group","").strip() or None,
            "location_name":         data.get("location_name","").strip() or None,
            "inventory_frequency":   data.get("inventory_frequency","weekly"),
            "inventory_notes":       data.get("inventory_notes","").strip() or None,
            "food_cost_target":      float(data.get("food_cost_target", 30) or 30),
            "digest_day":      data.get("digest_day","monday"),
            "digest_enabled":  int(data.get("digest_enabled",1)),
            "reviews_live":    int(bool(data.get("reviews_live"))),
            "billing_status":  data.get("billing_status","trial"),
            "internal_notes":  data.get("internal_notes","").strip() or None,
        })
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/admin/reset-password/<int:user_id>", methods=["POST"])
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

@app.route("/admin/reset-password-by-restaurant/<int:restaurant_id>", methods=["POST"])
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

@app.route("/api/log-activity", methods=["POST"])
@login_required
def log_activity_route(current_user):
    from models import log_activity
    data = request.get_json()
    log_activity(current_user["restaurant_id"], data.get("tab",""))
    return jsonify(ok=True)

@app.route("/stripe-webhook", methods=["POST"])
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
                    if billing_reason in ("subscription_cycle", "subscription_create") and dict(row)["billing_status"] != "active":
                        updates["billing_status"] = "active"
                        print(f"Auto-activated billing_status for {email}")
                    update_restaurant(dict(row)["id"], updates)
                    print(f"Saved Stripe customer {customer_id} for {email}")
            except Exception as e:
                print(f"Failed to save Stripe customer ID: {e}")

    return jsonify(ok=True)

@app.route("/admin/resend-contract/<int:restaurant_id>", methods=["POST"])
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

@app.route("/admin/resend-payment/<int:restaurant_id>", methods=["POST"])
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
        return jsonify(ok=True)
    except Exception as e:
        print(f"Resend payment error: {e}")
        return jsonify(ok=False, error=str(e))

@app.route("/admin/seed-reviews/<int:restaurant_id>", methods=["POST"])
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

@app.route("/admin/fetch-reviews/<int:restaurant_id>", methods=["POST"])
@admin_required
def fetch_reviews_now(restaurant_id, current_user):
    """Manually trigger a review fetch for a specific restaurant."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")

    from fetcher import fetch_google, fetch_yelp, save_reviews
    reviews = []
    errors = []

    if restaurant.google_place_id and restaurant.reviews_live:
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
        return jsonify(ok=False, error="No platform IDs configured or reviews_live is off")

    new_count = save_reviews(reviews) if reviews else 0

    # Analyse new reviews
    from models import get_pending_analysis
    pending = get_pending_analysis(restaurant_id, limit=50)
    if pending:
        from analyser import analyse_review
        for r in pending:
            try:
                analyse_review(r.id, r.rating, r.text)
            except Exception as e:
                errors.append(f"Analysis error: {e}")

    # Draft responses
    from models import get_pending_drafts
    pending_drafts = get_pending_drafts(restaurant_id)
    if pending_drafts:
        from drafter import draft_response
        for r in pending_drafts:
            try:
                draft_response(r.id, r.rating, r.text, r.sentiment,
                              restaurant.name, restaurant.voice_notes or "")
            except Exception as e:
                errors.append(f"Draft error: {e}")

    return jsonify(ok=True, new_reviews=new_count, errors=errors)

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
        prompt = f"""Write a professional, warm restaurant response to this {sentiment_note} review.

Restaurant: {restaurant.name}
Voice guidance: {restaurant.voice_notes or "Warm, genuine, never corporate. Always invite guests back."}
Sign off as: {restaurant.sign_off_name or restaurant.name}
Never use: {restaurant.never_say or ""}

Review (rating: {r["rating"]}/5):
{r["text"]}

Write ONLY the response, no preamble. Keep it under 100 words. Sound like a real person, not a PR firm."""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
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

@app.route("/admin/view-as/<int:restaurant_id>")
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
    token = create_session(dict(user_row)["id"], days=1)
    resp = make_response(redirect("/"))
    resp.set_cookie("session_token", token, max_age=86400,
                    httponly=True, samesite="Lax")
    return resp

@app.route("/admin/stop-viewing")
def stop_viewing():
    """Return to admin — delete current session and redirect to admin login."""
    token = request.cookies.get("session_token")
    if token:
        delete_session(token)
    resp = make_response(redirect("/login?next=/admin"))
    resp.delete_cookie("session_token")
    return resp

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

@app.route("/docusign/callback")
@app.route("/docusign/callback2")
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

@app.route("/docusign/webhook", methods=["POST"])
def docusign_webhook():
    """Receive DocuSign connect notifications when envelope status changes."""
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
                except Exception as e:
                    print(f"Payment email failed after signing: {e}")

                # Send welcome email with credentials
                try:
                    tmp_pw = r.get("temp_password") or ""
                    print(f"Welcome email temp_password length: {len(tmp_pw)}, value: '{tmp_pw}'")
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
                except Exception as e:
                    print(f"Welcome email failed after signing: {e}")

        return jsonify(ok=True)
    except Exception as e:
        print(f"DocuSign webhook error: {e}")
        return jsonify(ok=True)  # Always return 200 to DocuSign

@app.route("/admin/inventory-template")
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

@app.route("/privacy")
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

@app.route("/api/dismiss-welcome", methods=["POST"])
@login_required
def dismiss_welcome(current_user):
    """Mark user as having seen welcome banner by updating last_login."""
    from auth import update_last_login
    update_last_login(current_user["id"])
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
    <p>The server ran into an issue. It's been logged and Will will look into it. Email <a href="mailto:will@cavnar.ai" style="color:#c84b2f">will@cavnar.ai</a> if it keeps happening.</p>
    <a href="/login" class="btn">Back to dashboard</a>
  </div>
</body>
</html>"""
    return Response(html, status=500, mimetype="text/html")

@app.route("/sitemap.xml")
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

@app.route("/robots.txt")
def robots():
    from flask import Response
    txt = """User-agent: *
Allow: /
Sitemap: https://cavnar.ai/sitemap.xml"""
    return Response(txt, mimetype='text/plain')

@app.route("/og-image.png")
def og_image():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "og-image.png")
    return send_file(path, mimetype="image/png")

@app.route("/favicon.ico")
def favicon_ico():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "favicon.ico")
    return send_file(path, mimetype="image/x-icon")

@app.route("/favicon.png")
def favicon_png():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "favicon.png")
    return send_file(path, mimetype="image/png")

# ── Instagram / Meta routes ───────────────────────────────────────────────────

@app.route("/instagram/connect")
@login_required
def instagram_connect(current_user):
    """Redirect client to Meta OAuth to connect their Instagram."""
    import urllib.parse
    from flask import redirect as flask_redirect
    app_id       = os.getenv("META_APP_ID","")
    redirect_uri = "https://web-production-5d9dc.up.railway.app/instagram/callback"
    scope        = "instagram_basic,instagram_content_publish,pages_read_engagement,pages_show_list"
    state        = str(current_user["restaurant_id"])
    params = urllib.parse.urlencode({
        "client_id":     app_id,
        "redirect_uri":  redirect_uri,
        "scope":         scope,
        "response_type": "code",
        "state":         state,
    })
    return flask_redirect(f"https://www.facebook.com/v19.0/dialog/oauth?{params}")

@app.route("/instagram/callback")
def instagram_callback():
    """Handle Meta OAuth callback — exchange code for token, get IG user ID."""
    import requests as _req
    from flask import redirect as _ig_redirect
    from models import update_restaurant as _update_r

    code         = request.args.get("code")
    state        = request.args.get("state")
    app_id       = os.getenv("META_APP_ID","")
    app_secret   = os.getenv("META_APP_SECRET","")
    redirect_uri = "https://web-production-5d9dc.up.railway.app/instagram/callback"

    if not code:
        return _ig_redirect("/?ig_error=no_code")

    # Exchange code for short-lived token
    r = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": redirect_uri, "code": code,
    })
    if r.status_code != 200:
        print(f"IG token exchange failed: {r.text}")
        return _ig_redirect("/?ig_error=token_failed")
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
        return _ig_redirect("/?ig_error=no_ig_account")

    rid = int(state) if state and state.isdigit() else None
    if rid:
        update_data = {"ig_token": page_token, "ig_user_id": ig_user_id}
        # Also save Facebook page token/id if we found a page
        if pages:
            update_data["fb_page_token"] = pages[0].get("access_token", long_token)
            update_data["fb_page_id"]    = pages[0].get("id","")
        _update_r(rid, update_data)
        print(f"Instagram+Facebook connected for restaurant {rid}, ig_user_id={ig_user_id}")

    return _ig_redirect("/?ig_connected=1")

@app.route("/api/post-to-instagram", methods=["POST"])
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

@app.route("/api/instagram-status")
@login_required
def instagram_status(current_user):
    """Check if Instagram is connected for this restaurant."""
    restaurant = get_restaurant(current_user["restaurant_id"])
    connected    = bool(restaurant and restaurant.ig_token and restaurant.ig_user_id)
    fb_connected = bool(restaurant and restaurant.fb_page_token and restaurant.fb_page_id)
    return jsonify(connected=connected, fb_connected=fb_connected)

@app.route("/api/instagram-disconnect", methods=["POST"])
@login_required
def instagram_disconnect(current_user):
    """Disconnect Instagram from this restaurant."""
    from models import update_restaurant
    update_restaurant(current_user["restaurant_id"], {"ig_token": "", "ig_user_id": "", "fb_page_token": "", "fb_page_id": ""})
    return jsonify(ok=True)

@app.route("/api/post-to-facebook", methods=["POST"])
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

@app.route("/api/mark-posted/<int:review_id>", methods=["POST"])
@login_required
def mark_posted(review_id, current_user):
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='posted' WHERE id=? AND restaurant_id=?",
                 (review_id, current_user["restaurant_id"]))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@app.route("/api/export-reviews")
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

@app.route("/api/labor-trend")
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

        # Group shifts into 4 weekly buckets
        today = datetime.now().date()
        weeks = []
        for w in range(3, -1, -1):
            week_end   = today - timedelta(days=today.weekday()) - timedelta(weeks=w-1)
            week_start = week_end - timedelta(days=6)
            sales_total = 0
            labor_total = 0
            for s in shifts:
                try:
                    d = datetime.strptime(str(s.get("date",""))[:10], "%Y-%m-%d").date()
                    if week_start <= d <= week_end:
                        sales_total += float(s.get("sales_that_day") or 0)
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

# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    init_auth()
    from models import init_staff_notes, ensure_columns, init_email_log
    init_staff_notes()
    ensure_columns()
    init_email_log()

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
        print(f"\n  Admin account created: {ADMIN_USERNAME} / {admin_pw}")
        print("  Change your password after first login!\n")

    print(f"\n  Hosted dashboard → http://localhost:{PORT}")
    print(f"  Admin panel      → http://localhost:{PORT}/admin\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
