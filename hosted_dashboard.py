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
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "will@cavnar.ai")

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
    <button class="tab active" onclick="switchTab('reviews',this)">Reviews <span class="badge">{{rstats.total}}</span></button>
    <button class="tab" onclick="switchTab('labor',this)">Labor</button>
    <button class="tab" onclick="switchTab('inventory',this)">Inventory</button>
    <button class="tab" onclick="switchTab('marketing',this)">Marketing</button>
    <button class="tab" onclick="switchTab('account',this)">Account</button>
  </nav>
</div>

<!-- REVIEWS -->
<div class="panel active" id="panel-reviews">
  <div class="stat-row">
    <div class="stat"><div class="stat-n">{{rstats.avg_rating}}</div><div class="stat-l">Avg rating</div></div>
    <div class="stat"><div class="stat-n">{{rstats.total}}</div><div class="stat-l">Total</div></div>
    <div class="stat ok"><div class="stat-n">{{rstats.positive}}</div><div class="stat-l">Positive</div></div>
    <div class="stat warn"><div class="stat-n">{{rstats.neutral}}</div><div class="stat-l">Neutral</div></div>
    <div class="stat hi"><div class="stat-n">{{rstats.negative}}</div><div class="stat-l">Negative</div></div>
    <div class="stat hi"><div class="stat-n">{{rstats.urgent}}</div><div class="stat-l">Urgent</div></div>
    <div class="stat warn"><div class="stat-n">{{rstats.awaiting_approval}}</div><div class="stat-l">To approve</div></div>
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
      <div class="draft-box">
        <div class="draft-lbl">Suggested response</div>
        <div class="draft-txt">{{r.draft_response}}</div>
        <div class="draft-actions">
          {% if r.response_status=='posted' %}
            <span style="font-size:11px;color:var(--green);font-weight:500">✓ Posted</span>
          {% elif r.response_status=='approved' %}
            <span class="btn btn-approved">✓ Approved</span>
            <button class="btn btn-skip" onclick="skipR({{r.id}})">Skip</button>
          {% else %}
            <button class="btn btn-approve" onclick="approveR({{r.id}})">✓ Approve</button>
            <button class="btn btn-skip" onclick="skipR({{r.id}})">Skip</button>
          {% endif %}
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
<div class="panel" id="panel-labor">
  <div class="stat-row">
    <div class="stat warn"><div class="stat-n">${{labor.total_labor_cost|int|format_num}}</div><div class="stat-l">Labor (2 wks)</div></div>
    <div class="stat"><div class="stat-n">${{labor.total_sales|int|format_num}}</div><div class="stat-l">Revenue (2 wks)</div></div>
    <div class="stat {{'hi' if labor.overall_labor_pct>32 else 'ok'}}"><div class="stat-n">{{labor.overall_labor_pct}}%</div><div class="stat-l">Labor %</div></div>
    <div class="stat ok"><div class="stat-n">${{labor.potential_savings|int|format_num}}</div><div class="stat-l">Saveable/mo</div></div>
    <div class="stat hi"><div class="stat-n">{{labor.overstaffed_days|length}}</div><div class="stat-l">Overstaffed</div></div>
  </div>
  <div class="insight"><div class="insight-lbl">AI consultant analysis</div><div class="insight-text insight-loading" id="labor-insight">Loading analysis…</div></div>
  <div class="two-col">
    <div>
      <div class="slabel">Overstaffed days</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Date</th><th>Day</th><th>Sales</th><th>Labor</th><th>%</th></tr></thead>
        <tbody>{% for d in labor.overstaffed_days %}<tr><td>{{d.date}}</td><td>{{d.day}}</td><td>${{d.sales|int|format_num}}</td><td>${{d.labor_cost|format_num}}</td><td><span class="pill pill-red">{{d.labor_pct}}%</span></td></tr>
        {% else %}<tr><td colspan="5" style="color:var(--ink3);font-style:italic;padding:10px">None flagged</td></tr>{% endfor %}
        </tbody></table></div>
    </div>
    <div>
      <div class="slabel">Labor % by day</div>
      <div class="card" style="padding:14px"><div class="day-bars" id="day-bars"></div>
        <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--ink3)">{% for d in ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'] %}<span>{{d}}</span>{% endfor %}</div>
        <div style="margin-top:6px;font-size:10px;color:var(--ink3)">Target 28–32% · <span style="color:var(--red)">Red = over</span></div>
      </div>
    </div>
  </div>
</div>

<!-- INVENTORY -->
<div class="panel" id="panel-inventory">
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
<div class="panel" id="panel-marketing">
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
<div class="panel" id="panel-account">
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
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600)}
function switchTab(n,btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+n).classList.add('active');btn.classList.add('active');
  if(n==='labor'&&!laborLoaded)loadLaborInsight();
  if(n==='inventory'&&!invLoaded)loadInvInsight();
  if(n==='labor')renderBars();
}
let rfilter='{{rfilter}}';
function setRF(f,btn){rfilter=f;document.querySelectorAll('.fpill').forEach(p=>p.classList.remove('active','active-red'));btn.classList.add(f==='urgent'?'active-red':'active');filterReviews()}
function filterReviews(){const q=document.getElementById('rsearch').value;window.location='/?filter='+rfilter+'&search='+encodeURIComponent(q)}
function approveR(id){fetch('/approve/'+id,{method:'POST'}).then(r=>r.json()).then(d=>{if(d.ok){document.getElementById('rc-'+id).classList.add('approved');document.querySelector('#rc-'+id+' .draft-actions').innerHTML='<span class="btn btn-approved">✓ Approved</span>';toast('Response approved')}})}
function skipR(id){fetch('/skip/'+id,{method:'POST'}).then(r=>r.json()).then(d=>{if(d.ok){document.getElementById('rc-'+id).style.opacity='.4';toast('Skipped')}})}
const dowData={{labor.dow_summary|tojson}};
function renderBars(){const days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];const vals=days.map(d=>dowData[d]||0);const mx=Math.max(...vals,40);const c=document.getElementById('day-bars');if(!c)return;c.innerHTML=days.map(d=>{const pct=dowData[d]||0;const h=Math.round((pct/mx)*72);const col=pct>32?'var(--red)':pct>26?'var(--amber)':'var(--green)';return`<div class="day-bar-wrap"><div class="day-bar" style="height:${h}px;background:${col}" title="${d}: ${pct}%"></div></div>`}).join('')}
let laborLoaded=false,invLoaded=false;
function loadLaborInsight(){laborLoaded=true;fetch('/api/labor-insight').then(r=>r.json()).then(d=>{const el=document.getElementById('labor-insight');el.textContent=d.insight;el.classList.remove('insight-loading')})}
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
.tbl td{padding:10px 12px;border-bottom:1px solid var(--paper3)}
.tbl tr:last-child td{border-bottom:none}
.badge-active{background:var(--green-bg);color:var(--green);font-size:10px;padding:2px 7px;border-radius:20px;font-weight:500}
.status-msg{padding:10px 14px;border-radius:6px;font-size:13px;margin-top:10px;display:none}
.status-ok{background:var(--green-bg);color:var(--green);border:1px solid #b7dfca}
.status-err{background:#fdf0ef;color:var(--ember);border:1px solid #f5c6c2}
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
      <div class="form-group"><label>Temporary password</label><input type="text" id="u-password" placeholder="Set a temp password"></div>
      <div class="form-group"><label>Google Place ID (optional)</label><input type="text" id="r-google" placeholder="ChIJ..."></div>
      <div class="form-group"><label>Yelp Business ID (optional)</label><input type="text" id="r-yelp" placeholder="restaurant-name-chicago"></div>
      <div class="form-group full"><label>Owner voice notes (for AI drafting)</label><input type="text" id="r-voice" placeholder="Warm, casual tone. Always invite guests back. Never sound corporate."></div>
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
  <div class="card" style="padding:0;overflow:hidden">
    <table class="tbl">
      <thead><tr><th>Restaurant</th><th>Username</th><th>Email</th><th>Created</th><th>Last login</th><th>Status</th></tr></thead>
      <tbody>
      {% for user in users %}
      <tr>
        <td><strong>{{user.restaurant_name}}</strong></td>
        <td><code style="font-size:12px">{{user.username}}</code></td>
        <td>{{user.email}}</td>
        <td>{{user.created_at[:10]}}</td>
        <td>{{user.last_login[:10] if user.last_login else '—'}}</td>
        <td><span class="badge-active">{{'Active' if user.is_active else 'Inactive'}}</span></td>
      </tr>
      {% else %}
      <tr><td colspan="6" style="color:var(--ink3);font-style:italic;padding:16px">No clients yet — create one above.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<script>
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
    voice_notes:     document.getElementById('r-voice').value,
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
    status.textContent = `✓ Client created — username: ${payload.username}${payload.send_email ? ". Welcome email sent to " + payload.owner_email : ""}`;
    setTimeout(() => location.reload(), 1500);
  } else {
    status.className = 'status-msg status-err';
    status.textContent = data.error || 'Something went wrong';
  }
  btn.textContent = 'Create client account'; btn.disabled = false;
}
</script>
</body>
</html>"""


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
    labor   = analyse_shifts(load_shifts())
    inv     = analyse_inventory(load_inventory())
    return render_template_string(DASHBOARD_HTML,
        current_user=current_user, restaurant=restaurant,
        rstats=rstats, reviews=reviews, rfilter=rfilter, rsearch=rsearch,
        labor=labor, inv=inv, ctypes=CONTENT_TYPES,
        now=datetime.now().strftime("%b %d, %Y"))

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
    from labor import load_shifts, analyse_shifts, get_claude_insights
    insight = get_claude_insights(analyse_shifts(load_shifts()))
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
    return jsonify(content=generate_content(
        data.get("type","instagram_post"), data.get("topic","")))

@app.route("/api/content-calendar")
@login_required
def content_calendar(current_user):
    from marketing import get_content_calendar_ideas
    return jsonify(ideas=get_content_calendar_ideas())

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
    return render_template_string(ADMIN_HTML,
        current_user=current_user, users=users)

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
        ))
        # Create user
        create_user(
            restaurant_id=rid,
            username=data["username"],
            email=data["owner_email"],
            password=data["password"],
        )
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
        return jsonify(ok=True, restaurant_id=rid)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    init_auth()

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
