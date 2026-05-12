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
                   jsonify, redirect, url_for, make_response)
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
<title>Cavnar AI — Sign in</title>
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
        <div class="card-author">{{r.author}}</div>
        <div class="card-sub">
          <span class="stars">{% for i in range(5) %}{{('★' if i<r.rating else '☆')}}{% endfor %}</span>
          <span class="pbadge {{'pg' if r.platform=='google' else 'py'}}">{{r.platform}}</span>
          {% if r.review_date %}<span>{{r.review_date[:10]}}</span>{% endif %}
        </div>
      </div>
      <span class="schip {{'sp' if r.sentiment=='positive' else ('sn' if r.sentiment=='negative' else 'su')}}">{{r.sentiment or 'neutral'}}</span>
    </div>
    <div class="card-body">
      <div class="rtext">{{r.text}}</div>
      {% if r.categories %}<div class="cats">{% for c in r.categories %}<span class="cat">{{c.replace('_',' ')}}</span>{% endfor %}</div>{% endif %}
      {% if r.draft_response %}
      <div class="draft-box" id="draft-box-{{r.id}}">
        <div class="draft-lbl">Suggested response</div>
        <div class="draft-txt" id="draft-txt-{{r.id}}">{{r.draft_response}}</div>
        <div class="draft-actions" id="draft-actions-{{r.id}}">
          {% if r.response_status=='posted' %}
            <span style="font-size:11px;color:var(--green);font-weight:500">✓ Posted</span>
          {% elif r.response_status=='approved' %}
            <span class="btn btn-approved">✓ Approved</span>
            <button class="btn btn-skip" onclick="skipR({{r.id}})">Edit</button>
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
          <textarea id="editor-text-{{r.id}}" style="width:100%;padding:8px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--ink);background:white;resize:vertical;min-height:90px;outline:none" placeholder="Write your own response…">{{r.draft_response}}</textarea>
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

  <!-- Hero metric — dollar gap -->
  <div id="labor-gap-banner" style="background:var(--ink);border-radius:var(--r);padding:20px 24px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
    <div>
      <div style="font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);margin-bottom:6px">Monthly labor cost vs target</div>
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
          <td style="color:var(--red);font-size:11px;font-weight:500">+{{(d.labor_pct - 30)|round(1)}}pp</td>
        </tr>
        {% else %}
        <tr><td colspan="6" style="color:var(--ink3);font-style:italic;padding:10px">No overstaffed days — great work!</td></tr>
        {% endfor %}
        </tbody>
      </table></div>

      {% if labor.overtime_risk %}
      <div class="slabel" style="margin-top:14px">Overtime risk</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Employee</th><th>Hours (2 wks)</th><th>Status</th></tr></thead>
        <tbody>
        {% for emp in labor.overtime_risk %}
        <tr>
          <td style="font-weight:500">{{emp.employee}}</td>
          <td>{{emp.hours}}h</td>
          <td><span class="pill pill-amber">Near overtime</span></td>
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
        <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--ink3);margin-top:4px">
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
</div>

<!-- INVENTORY -->
<div class="panel {{'active' if not mod_reviews and not mod_labor and mod_inventory}}" id="panel-inventory">
  <div class="stat-row">
    <div class="stat hi"><div class="stat-n">${{inv.total_waste_cost_week|format_num}}</div><div class="stat-l">Waste/week</div></div>
    <div class="stat hi"><div class="stat-n">${{inv.monthly_waste_projection|int|format_num}}</div><div class="stat-l">Projected/mo</div></div>
    <div class="stat ok"><div class="stat-n">${{inv.recoverable_monthly|int|format_num}}</div><div class="stat-l">Recoverable</div></div>
    <div class="stat warn"><div class="stat-n">{{inv.waste_items|length}}</div><div class="stat-l">Waste items</div></div>
    <div class="stat hi"><div class="stat-n">{{inv.critical_low|length}}</div><div class="stat-l">Critical low</div></div>
  </div>
  <div class="insight"><div class="insight-lbl">AI food cost analysis</div><div class="insight-text insight-loading" id="inv-insight">Loading analysis…</div></div>
  <div class="two-col">
    <div>
      <div class="slabel">Top waste offenders</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Item</th><th>Wasted</th><th>Cost</th><th>%</th></tr></thead>
        <tbody>{% for item in inv.waste_items %}<tr>
          <td><strong>{{item.item}}</strong></td><td>{{item.waste_last_week}} {{item.unit}}</td>
          <td><span class="pill pill-red">${{item.waste_cost}}</span></td><td>{{item.waste_pct}}%</td>
        </tr>{% endfor %}</tbody></table></div>
    </div>
    <div>
      <div class="slabel">Overstocked</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Item</th><th>Stock</th><th>Par</th><th>Excess</th></tr></thead>
        <tbody>{% for item in inv.overstock %}<tr>
          <td><strong>{{item.item}}</strong></td><td>{{item.current_stock}}</td>
          <td>{{item.par_level}}</td><td><span class="pill pill-amber">${{item.overstock_cost}}</span></td>
        </tr>{% else %}<tr><td colspan="4" style="color:var(--ink3);font-style:italic;padding:10px">None flagged</td></tr>{% endfor %}</tbody></table></div>
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
  <div class="slabel">Content type</div>
  <div class="ct-grid">{% for ct in ctypes %}
    <div class="ct-btn {{'selected' if loop.first}}" onclick="selectCt('{{ct.id}}',this)">
      <div class="ct-label">{{ct.label}}</div><div class="ct-desc">{{ct.description}}</div>
    </div>{% endfor %}
  </div>
  <div class="topic-row">
    <input class="topic-input" id="mktopic" placeholder="Topic or occasion — e.g. new spring menu, Mother's Day brunch…" value="New spring menu launch">
    <button class="btn-primary" onclick="genContent()">Generate ↗</button>
  </div>
  <div class="output-box" id="mkoutput" style="color:var(--ink3);font-style:italic">Select a type and click Generate.</div>
  <div style="display:flex;gap:6px;margin-top:8px">
    <button class="btn-secondary" onclick="navigator.clipboard.writeText(document.getElementById('mkoutput').textContent).then(()=>toast('Copied'))">Copy</button>
    <button class="btn-secondary" onclick="genContent()">Regenerate</button>
  </div>
  <div style="margin-top:24px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
      <div class="slabel" style="margin:0">Content calendar</div>
      <button class="btn-secondary" style="font-size:10px;padding:5px 10px" onclick="loadCal()">Generate week ↗</button>
    </div>
    <div class="cal-grid" id="cal-grid"><div class="no-data" style="grid-column:1/-1;padding:20px">Click "Generate week" for content ideas.</div></div>
  </div>
</div>

<!-- ACCOUNT -->
<div class="panel {{'active' if not mod_reviews and not mod_labor and not mod_inventory and not mod_marketing}}" id="panel-account">
  <div class="slabel">Restaurant details</div>
  <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:16px;max-width:500px">
    <table style="font-size:13px;width:100%">
      <tr><td style="color:var(--ink3);padding:5px 0;width:140px">Restaurant</td><td style="font-weight:500">{{restaurant.name}}</td></tr>
      <tr><td style="color:var(--ink3);padding:5px 0">Owner email</td><td>{{restaurant.owner_email}}</td></tr>
      <tr><td style="color:var(--ink3);padding:5px 0">Support</td><td><a href="mailto:will@cavnar.ai" style="color:var(--ember);text-decoration:none">will@cavnar.ai</a></td></tr>
      <tr><td style="color:var(--ink3);padding:5px 0">Powered by</td><td><a href="https://cavnar.ai" target="_blank" style="color:var(--ember);text-decoration:none">cavnar.ai</a></td></tr>
    </table>
  </div>
  <div class="slabel">Change password</div>
  <div class="change-pw-section">
    <div class="form-group"><label class="form-label">Current password</label><input class="form-input" type="password" id="pw-current" placeholder="••••••••"></div>
    <div class="form-group"><label class="form-label">New password</label><input class="form-input" type="password" id="pw-new" placeholder="min 8 characters"></div>
    <div class="form-group"><label class="form-label">Confirm new password</label><input class="form-input" type="password" id="pw-confirm" placeholder="••••••••"></div>
    <button class="btn-primary" onclick="changePassword()">Update password</button>
    <div id="pw-status" style="font-size:12px;margin-top:8px;display:none"></div>
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
  if(n==='inventory'&&!invLoaded)loadInvInsight();
  if(n==='labor')renderBars();
  fetch('/api/log-activity',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tab:n})});
}
// Auto-load data for whichever tab is active on page load
window.addEventListener('DOMContentLoaded', function() {
  const activePanel = document.querySelector('.panel.active');
  if(!activePanel) return;
  const id = activePanel.id.replace('panel-','');
  if(id==='labor'&&!laborLoaded){loadLaborInsight();renderBars();}
  if(id==='inventory'&&!invLoaded){loadInvInsight();}
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
function renderBars(){const days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];const vals=days.map(d=>dowData[d]||0);const mx=Math.max(...vals,40);const c=document.getElementById('day-bars');if(!c)return;c.innerHTML=days.map(d=>{const pct=dowData[d]||0;const h=Math.round((pct/mx)*72);const col=pct>32?'var(--red)':pct>26?'var(--amber)':'var(--green)';return`<div class="day-bar-wrap"><div class="day-bar" style="height:${h}px;background:${col}" title="${d}: ${pct}%"></div></div>`}).join('')}
let laborLoaded=false,invLoaded=false;
function loadLaborInsight(){
  laborLoaded=true;
  // Load AI insight
  fetch('/api/labor-insight').then(r=>r.json()).then(d=>{
    const el=document.getElementById('labor-insight');
    el.textContent=d.insight;
    el.classList.remove('insight-loading');
  });
  // Load dollar gap
  fetch('/api/labor-gap').then(r=>r.json()).then(d=>{
    const gapEl = document.getElementById('gap-amount');
    const msgEl = document.getElementById('gap-dollar');
    const pctEl = document.getElementById('gap-current-pct');
    if(d.over_target && d.monthly_gap > 0) {
      gapEl.textContent = '$' + d.monthly_gap.toLocaleString();
      gapEl.style.color = 'var(--ember2)';
      msgEl.textContent = 'You are ' + d.current_pct + '% — ' + d.monthly_gap.toLocaleString(undefined,{style:"currency",currency:"USD",maximumFractionDigits:0}) + '/mo above the 30% target. Optimizing scheduling could recover this.';
      pctEl.style.color = '#ef9f27';
    } else {
      gapEl.textContent = 'On target';
      gapEl.style.color = '#6fcf97';
      msgEl.textContent = 'Your labor % is within the 28-32% target range. Keep it up.';
      pctEl.style.color = '#6fcf97';
    }
  });
}
async function downloadSchedule(btn) {
  btn.textContent = 'Generating…';
  btn.disabled = true;
  try {
    const res = await fetch('/api/download-schedule');
    if(!res.ok) { btn.textContent = 'Error — try again'; btn.disabled=false; return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'optimized_schedule.csv'; a.click();
    btn.textContent = '✓ Downloaded';
    setTimeout(()=>{btn.textContent='Download optimized schedule ↓';btn.disabled=false;},3000);
  } catch(e) {
    btn.textContent = 'Error — try again';
    btn.disabled = false;
  }
}
function loadInvInsight(){invLoaded=true;fetch('/api/inv-insight').then(r=>r.json()).then(d=>{const el=document.getElementById('inv-insight');el.textContent=d.insight;el.classList.remove('insight-loading')})}
let selCt='{{ctypes[0].id if ctypes}}';
function selectCt(id,el){selCt=id;document.querySelectorAll('.ct-btn').forEach(b=>b.classList.remove('selected'));el.classList.add('selected')}
function genContent(){const topic=document.getElementById('mktopic').value.trim();if(!topic){toast('Enter a topic');return}const box=document.getElementById('mkoutput');box.style.fontStyle='italic';box.style.color='var(--ink3)';box.textContent='Generating…';fetch('/api/generate-content',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:selCt,topic})}).then(r=>r.json()).then(d=>{box.style.fontStyle='normal';box.style.color='var(--ink2)';box.textContent=d.content})}
function loadCal(){const g=document.getElementById('cal-grid');g.innerHTML='<div class="no-data" style="grid-column:1/-1;padding:16px">Generating…</div>';fetch('/api/content-calendar').then(r=>r.json()).then(d=>{if(!d.ideas||!d.ideas.length){g.innerHTML='<div class="no-data" style="grid-column:1/-1">Could not generate.</div>';return}g.innerHTML=d.ideas.map(i=>`<div class="cal-day"><div class="cal-day-name">${i.day}</div><div class="cal-platform">${i.platform||''}</div><div>${i.angle||''}</div></div>`).join('')})}
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
          <label>POS system</label>
          <select id="pos_system">
            <option value="">Unknown / not set</option>
            {% for pos in ['Toast','Square','Lightspeed','Aloha / NCR','Clover','Revel','TouchBistro','Other / Manual'] %}
            <option value="{{ pos }}" {{'selected' if restaurant.pos_system == pos}}>{{ pos }}</option>
            {% endfor %}
          </select>
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
      </div>
    </div>
  </div>

  <!-- Service tier -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Service tier</div></div>
    <div class="section-body">
      <div class="form-grid">
        <div class="form-group">
          <label>Plan</label>
          <select id="service_tier" onchange="updateTierPreview(this.value)">
            <option value="trial" {{"selected" if restaurant.service_tier == "trial"}}>Trial — all modules (demo)</option>
            <option value="starter_reviews" {{"selected" if restaurant.service_tier == "starter_reviews"}}>Starter — Review Intelligence only</option>
            <option value="starter_labor" {{"selected" if restaurant.service_tier == "starter_labor"}}>Starter — Labor Optimizer only</option>
            <option value="starter_inventory" {{"selected" if restaurant.service_tier == "starter_inventory"}}>Starter — Inventory Control only</option>
            <option value="starter_marketing" {{"selected" if restaurant.service_tier == "starter_marketing"}}>Starter — Marketing Autopilot only</option>
            <option value="full" {{"selected" if restaurant.service_tier == "full"}}>Full System — all 4 modules</option>
          </select>
          <div class="hint">Module tabs update automatically when you save. No manual toggles needed.</div>
        </div>
        <div style="background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;padding:12px 14px">
          <div style="font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--ink3);margin-bottom:8px">Active modules</div>
          <div id="tier-preview" style="display:flex;flex-direction:column;gap:5px;font-size:12px"></div>
        </div>
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

const TIER_MODULES = {
  "trial":             {reviews:1,labor:1,inventory:1,marketing:1},
  "starter_reviews":   {reviews:1,labor:0,inventory:0,marketing:0},
  "starter_labor":     {reviews:0,labor:1,inventory:0,marketing:0},
  "starter_inventory": {reviews:0,labor:0,inventory:1,marketing:0},
  "starter_marketing": {reviews:0,labor:0,inventory:0,marketing:1},
  "full":              {reviews:1,labor:1,inventory:1,marketing:1},
};
const MODULE_LABELS = {
  reviews:"Review Intelligence",labor:"Labor Optimizer",
  inventory:"Inventory Control",marketing:"Marketing Autopilot"
};
function updateTierPreview(tier) {
  const mods = TIER_MODULES[tier] || TIER_MODULES["trial"];
  const el = document.getElementById("tier-preview");
  el.innerHTML = Object.entries(mods).map(([k,v]) =>
    `<div style="display:flex;align-items:center;gap:6px">
      <span style="width:8px;height:8px;border-radius:50%;background:${v?"#2d6a4f":"#e0dbd0"}"></span>
      <span style="color:${v?"#0e0c0a":"#7a736a"}">${MODULE_LABELS[k]}</span>
    </div>`
  ).join("");
}
// Init preview on load
updateTierPreview(document.getElementById("service_tier").value);

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

async function saveSettings() {
  const btn = document.querySelector('.btn-save');
  const status = document.getElementById('save-status');
  btn.textContent = 'Saving…'; btn.disabled = true;
  const payload = {
    name:            document.getElementById('name').value,
    owner_email:     document.getElementById('owner_email').value,
    owner_name:      document.getElementById('owner_name').value,
    owner_phone:     document.getElementById('owner_phone').value,
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
    hourly_rate:     parseFloat(document.getElementById('hourly_rate').value),
    billing_status:  document.getElementById('billing_status').value,
    internal_notes:  document.getElementById('internal_notes').value,
    service_tier:    document.getElementById('service_tier').value,
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

  <div class="section-title">Create new client account</div>
  <div class="card">
    <div class="form-grid">
      <div class="form-group"><label>Restaurant name</label><input type="text" id="r-name" placeholder="Maplewood Kitchen"></div>
      <div class="form-group"><label>Owner email</label><input type="email" id="r-email" placeholder="owner@restaurant.com"></div>
      <div class="form-group"><label>Dashboard username</label><input type="text" id="u-username" placeholder="maplewoodkitchen"></div>
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
      <div class="form-group"><label>Google Place ID (optional)</label><input type="text" id="r-google" placeholder="ChIJ..."></div>
      <div class="form-group"><label>Yelp Business ID (optional)</label><input type="text" id="r-yelp" placeholder="restaurant-name-chicago"></div>
      <div class="form-group"><label>Owner / GM name</label><input type="text" id="r-owner-name" placeholder="e.g. Sarah"></div>
      <div class="form-group"><label>Owner phone number</label><input type="text" id="r-phone" placeholder="(312) 555-0100"></div>
      <div class="form-group full"><label>Owner voice notes (for AI drafting)</label><input type="text" id="r-voice" placeholder="Warm, casual tone. Always invite guests back. Never sound corporate."></div>
      <div class="form-group">
        <label>Service tier</label>
        <select id="r-tier">
          <option value="trial">Trial — all modules (demo)</option>
          <option value="starter_reviews">Starter — Review Intelligence</option>
          <option value="starter_labor">Starter — Labor Optimizer</option>
          <option value="starter_inventory">Starter — Inventory Control</option>
          <option value="starter_marketing">Starter — Marketing Autopilot</option>
          <option value="full">Full System — all 4 modules</option>
        </select>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px;margin-top:14px;margin-bottom:0">
      <input type="checkbox" id="send-email" checked style="width:16px;height:16px;accent-color:#c84b2f;cursor:pointer">
      <label for="send-email" style="font-size:13px;color:#3a3530;cursor:pointer;letter-spacing:0;text-transform:none;font-weight:400">
        Send welcome email to owner with login credentials
      </label>
    </div>
    <button class="btn btn-primary" style="margin-top:12px" onclick="createClient()">Create client account</button>
    <div class="status-msg" id="create-status"></div>
  </div>

  <div class="section-title">Active client accounts</div>
  <div class="card" style="padding:0;overflow:visible">
    <table class="tbl">
      <thead><tr><th>Restaurant</th><th>Username</th><th>Email</th><th>Phone</th><th>Billing</th><th>Last login</th><th>Last tab</th><th>Last fetched</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>
      {% for user in users %}
      <tr>
        <td><strong>{{user.restaurant_name}}</strong></td>
        <td><code style="font-size:12px">{{user.username}}</code></td>
        <td>{{user.email}}</td>
        <td style="font-size:12px;color:var(--ink3)">{{user.phone or '—'}}</td>
        <td>
          {% set bc = {'trial':'#b7791f','active':'#2d6a4f','paused':'#6b7280','churned':'#c0392b'} %}
          <span style="font-size:10px;font-weight:500;padding:2px 8px;border-radius:20px;background:{% if user.billing_status == 'active' %}var(--green-bg){% elif user.billing_status == 'trial' %}var(--amber-bg){% else %}#f3f4f6{% endif %};color:{{ bc.get(user.billing_status,'#6b7280') }}">
            {{(user.billing_status or 'trial')|title}}
          </span>
        </td>
        <td style="font-size:12px">{{user.last_login[:10] if user.last_login else '—'}}</td>
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
    service_tier:    document.getElementById('r-tier').value,
    send_email:      document.getElementById('send-email').checked,
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
</script>
</body>
</html>"""


PAYMENT_LINKS = {
    "trial":             None,
    "starter_reviews":   "https://checkout.cavnar.ai/b/5kQ9AV4Fh5Z68VadU74Ni04",
    "starter_labor":     "https://checkout.cavnar.ai/b/5kQ9AV4Fh5Z68VadU74Ni04",
    "starter_inventory": "https://checkout.cavnar.ai/b/5kQ9AV4Fh5Z68VadU74Ni04",
    "starter_marketing": "https://checkout.cavnar.ai/b/5kQ9AV4Fh5Z68VadU74Ni04",
    "full":              "https://checkout.cavnar.ai/b/aFa00l4Fhcnu9Ze03h4Ni05",
}

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


def send_payment_email(to_email, restaurant_name, tier):
    """Send setup + retainer payment links based on service tier."""
    if not RESEND_API_KEY:
        return
    link   = PAYMENT_LINKS.get(tier)
    prices = TIER_PRICES.get(tier, {})
    label  = TIER_LABELS.get(tier, tier)

    if not link:
        return  # Trial — no payment needed

    setup_price    = prices.get("setup", "")
    retainer_price = prices.get("retainer", "")

    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY
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
    One checkout handles everything — your {setup_price} setup fee today,
    then your {retainer_price} retainer starts automatically in 30 days.
    No second step needed.
  </p>

  <div style="background:#f7f4ef;border-radius:8px;padding:20px 22px;margin-bottom:24px;border-left:3px solid #c84b2f">
    <p style="font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin:0 0 6px">{label}</p>
    <div style="display:flex;gap:20px;margin-bottom:12px">
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
    <a href="{link}"
       style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:.04em">
      Complete payment →
    </a>
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


def send_welcome_email(to_email, restaurant_name, username, password):
    """Send branded welcome email to new client with their login credentials."""
    import resend as _resend
    _resend.api_key = RESEND_API_KEY
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
    Your dashboard includes four modules — Reviews, Labor, Inventory, and Marketing —
    all set up specifically for {restaurant_name}.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:24px">
    Any questions, just reply to this email. I check it daily.
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
    if current_user["is_admin"]:
        return redirect("/admin")
    from labor import load_shifts, analyse_shifts
    from inventory import load_inventory, analyse_inventory
    from marketing import CONTENT_TYPES
    rid     = current_user["restaurant_id"]
    rfilter = request.args.get("filter","all")
    rsearch = request.args.get("search","")
    restaurant = get_restaurant(rid)
    rstats  = get_review_stats(rid)
    reviews = get_reviews_data(rid, rfilter, rsearch)
    from labor import analyse_shifts_for_restaurant
    from inventory import load_inventory_for_restaurant, analyse_inventory
    labor   = analyse_shifts_for_restaurant(rid)
    inv     = analyse_inventory(load_inventory_for_restaurant(rid))
    from marketing import CONTENT_TYPES
    return render_template_string(DASHBOARD_HTML,
        current_user=current_user, restaurant=restaurant,
        rstats=rstats, reviews=reviews, rfilter=rfilter, rsearch=rsearch,
        labor=labor, inv=inv, ctypes=CONTENT_TYPES,
        mod_reviews=restaurant.module_reviews,
        mod_labor=restaurant.module_labor,
        mod_inventory=restaurant.module_inventory,
        mod_marketing=restaurant.module_marketing,
        now=datetime.now().strftime("%b %d, %Y"),
        viewing_as=current_user.get("is_admin", 0))

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
    from labor import analyse_shifts_for_restaurant, get_claude_insights
    from models import get_restaurant
    restaurant = get_restaurant(current_user["restaurant_id"])
    name = restaurant.name if restaurant else "your restaurant"
    owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
    analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
    insight = get_claude_insights(analysis, restaurant_name=name, owner_name=owner)
    return jsonify(insight=insight)

@app.route("/api/inv-insight")
@login_required
def inv_insight_api(current_user):
    from inventory import load_inventory, analyse_inventory, get_claude_insights
    insight = get_claude_insights(analyse_inventory(load_inventory()))
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
        enriched.append(u)
    return render_template_string(ADMIN_HTML,
        current_user=current_user, users=enriched)

@app.route("/admin/create-client", methods=["POST"])
@admin_required
def create_client(current_user):
    from models import create_restaurant, Restaurant
    data = request.get_json()
    try:
        # Create restaurant
        rid = create_restaurant(Restaurant(
            name=data["restaurant_name"],
            owner_email=data["owner_email"],
            google_place_id=data.get("google_place_id") or None,
            yelp_business_id=data.get("yelp_business_id") or None,
            voice_notes=data.get("voice_notes") or None,
            owner_phone=data.get("owner_phone") or None,
            owner_name=data.get("owner_name") or None,
        ))
        # Create user
        create_user(
            restaurant_id=rid,
            username=data["username"],
            email=data["owner_email"],
            password=data["password"],
        )
        # Auto-set service tier if provided
        if data.get("service_tier"):
            from models import set_service_tier
            set_service_tier(rid, data["service_tier"])
        # Send welcome email if requested
        if data.get("send_email") and RESEND_API_KEY:
            try:
                send_welcome_email(
                    to_email=data["owner_email"],
                    restaurant_name=data["restaurant_name"],
                    username=data["username"],
                    password=data["password"],
                )
            except Exception as mail_err:
                print(f"Welcome email failed: {mail_err}")
        # Always send payment email (unless trial)
        tier = data.get("service_tier","trial")
        if tier != "trial" and RESEND_API_KEY:
            try:
                send_payment_email(
                    to_email=data["owner_email"],
                    restaurant_name=data["restaurant_name"],
                    tier=tier,
                )
            except Exception as mail_err:
                print(f"Payment email failed: {mail_err}")
        return jsonify(ok=True, restaurant_id=rid)
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
    from models import get_client_data
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    data = get_client_data(restaurant_id) or {}
    return render_template_string(CLIENT_DATA_HTML,
        current_user=current_user,
        restaurant=restaurant,
        data=data)

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
    return render_template_string(CLIENT_SETTINGS_HTML,
        current_user=current_user,
        restaurant=restaurant,
        client_data=client_data)

@app.route("/admin/client-settings/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_client_settings(restaurant_id, current_user):
    from models import update_restaurant
    data = request.get_json()
    try:
        from models import set_service_tier
        tier = data.get("service_tier","trial")
        # Set tier first (auto-configures modules)
        set_service_tier(restaurant_id, tier)
        # Then save all other fields
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
            "pos_system":      data.get("pos_system","").strip() or None,
            "owner_name":      data.get("owner_name","").strip() or None,
            "owner_phone":     data.get("owner_phone","").strip() or None,
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
        # Payment succeeded — log it (no action needed)
        inv   = event["data"]["object"]
        email = inv.get("customer_email","unknown")
        amount = inv.get("amount_paid", 0) / 100
        print(f"Payment received: {email} — ${amount:.2f}")

    return jsonify(ok=True)

@app.route("/admin/resend-payment/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_payment(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        send_payment_email(
            to_email=restaurant.owner_email,
            restaurant_name=restaurant.name,
            tier=restaurant.service_tier or "trial",
        )
        return jsonify(ok=True)
    except Exception as e:
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
    from labor import analyse_shifts_for_restaurant, calculate_monthly_gap
    analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
    gap = calculate_monthly_gap(analysis)
    return jsonify(gap)

@app.route("/api/download-schedule")
@login_required
def download_schedule(current_user):
    from labor import (analyse_shifts_for_restaurant, load_shifts_for_restaurant,
                       generate_optimized_schedule, get_hourly_rate)
    from models import get_restaurant
    import io
    restaurant = get_restaurant(current_user["restaurant_id"])
    shifts   = load_shifts_for_restaurant(current_user["restaurant_id"])
    analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
    rate     = get_hourly_rate(current_user["restaurant_id"])
    csv_text = generate_optimized_schedule(
        analysis, shifts,
        restaurant_name=restaurant.name if restaurant else "Restaurant",
        hourly_rate=rate
    )
    return send_file(
        io.BytesIO(csv_text.encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"optimized_schedule_{restaurant.name.replace(' ','_')}.csv"
    )

# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    init_auth()

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
