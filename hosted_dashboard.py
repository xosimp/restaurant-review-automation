"""
hosted_dashboard.py — Cavnar AI hosted client dashboard
Multi-client, login-protected, Railway-deployable

Run locally:  python3 hosted_dashboard.py
Deploy:       Railway (connect GitHub repo, set env vars)
"""
import os
import config
import pathlib
from datetime import datetime, timedelta
from functools import wraps
# Must run before any local module is imported below — emails.py (and
# others) read RESEND_API_KEY/etc. as a MODULE-LEVEL constant via
# os.getenv() at import time. Python only executes a module's top-level
# code on its FIRST import; every later `from emails import ...` anywhere
# in the app (admin_routes.py, webhook_routes.py) just reuses that same
# cached module object. Locally, .env is the only source for these vars —
# on Railway they're already in the real environment before Python even
# starts, so this ordering bug never showed up there — but here, the old
# order (load_dotenv() at the bottom of this import block, after `from
# emails import ...` above it) meant RESEND_API_KEY froze as "" for the
# life of every local run, and every emails.py function silently failed
# with no indication why. Found via send_2fa_code's new logging: "RESEND_
# API_KEY not set" even though .env plainly has a working key.
from dotenv import load_dotenv
load_dotenv(pathlib.Path(__file__).parent / ".env")
PORT = int(os.getenv("PORT", 5000))
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
from models import get_conn, get_restaurant, get_review_stats, get_reviews_data, get_top_issues, get_sentiment_trend, is_full_tier, get_active_modules
from auth import get_session_user, create_user

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

# Trust the one proxy hop in front of us (Railway's edge) for scheme, client
# address and host. See auth.install_proxy_fix for why this is a function.
try:
    from auth import install_proxy_fix as _install_proxy_fix
    _install_proxy_fix(app)
except Exception as _pf_e:  # pragma: no cover — werkzeug always ships this
    print(f"[boot] ProxyFix unavailable: {_pf_e}")

def _check_duplicate_routes():
    """Crash loudly at startup if any (path, method) is registered more than
    once. Werkzeug serves whichever rule was registered first, silently:
    the admin blueprint's permissive /robots.txt shadowed the app's for
    months. Written in June, never called until now. Counted per method so
    a GET and a POST on one path (registered as two rules) are not a
    duplicate."""
    from collections import Counter
    seen = Counter((r.rule, m) for r in app.url_map.iter_rules()
                   for m in (r.methods or ()) if m not in ("HEAD", "OPTIONS"))
    dupes = sorted({rule for (rule, _m), n in seen.items() if n > 1})
    if dupes:
        raise RuntimeError(f"DUPLICATE ROUTES DETECTED — fix before deploying: {dupes}")
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB global upload limit

from competitor_intel_format import (
    format_intel as _format_intel, format_intel_body as _format_intel_body,
    extract_recs as _extract_recs,
)

app.template_filter("format_intel")(_format_intel)
app.template_filter("extract_recs")(_extract_recs)
from competitor_intel_format import parse_competitor_intel as _parse_intel
app.template_filter("intel_parts")(lambda text: _parse_intel(text) if text else {"intro": "", "sections": [], "recommendations": []})
app.template_filter("format_intel_body")(_format_intel_body)


class _FoodCostWithheld(Exception):
    """This identity may not see food cost, so none of it is computed or
    rendered. Not an error — a deliberate, silent withholding."""


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
    """Health check for UptimeRobot and Railway.

    Checks the database AND the background scheduler's heartbeat. The
    scheduler runs as a thread inside this process and cannot notice its own
    death — so if it stops, nothing raises and nothing 500s. Scheduled
    marketing posts, nightly POS syncs and review fetches simply stop
    happening, quietly, and the first sign is a client asking why their post
    never appeared. Checking it from a REQUEST thread is the only place that
    can see it.

    Deliberately still 200 when the scheduler is stale, and likewise when
    the volume is filling: the web app is up and serving, and failing the
    healthcheck would put a deploy problem on top of a real one. The signal
    goes in the body and on the status page, where the operator digest and
    /status can act on it. The one 500 is an unreadable database — the case
    where a new deployment genuinely should not replace a working one.

    The body lives in status_manager.health_snapshot so it can be tested:
    importing THIS module boots the database, seeds demo data, starts the
    scheduler thread and re-runs csrf_protect on already-registered
    blueprints, which is the same reason http_layer.py was extracted.
    """
    from status_manager import health_snapshot
    payload, status = health_snapshot()
    if status != 200:
        return jsonify(**payload), status
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or ""
    if sha:
        payload["build"] = sha[:12]
    return jsonify(**payload), 200

@app.template_filter("format_num")
def format_num(v):
    try: return f"{float(v):,.0f}"
    except (TypeError, ValueError): return v

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

# Response compression and cache headers live in http_layer.py so they can be
# tested without booting this module (importing it initialises the database,
# seeds demo data and starts the scheduler thread).
import http_layer
http_layer.register(app)


@app.after_request
def add_security_headers(response):
    """Add security headers to every response (security_headers.py)."""
    import security_headers
    return security_headers.apply(response)

# Register blueprints
from admin_routes import admin_bp
from webhook_routes import webhook_bp
from social_routes import social_bp
from auth_routes import auth_bp
from client_api import client_bp
from toast_routes import toast_bp
from square_routes import square_bp
from clover_routes import clover_bp
from rpower_routes import rpower_bp
from status_routes import status_bp
from mobile_api import mobile_bp
from csrf import csrf_protect, ensure_csrf_cookie

# Every browser-facing blueprint gets double-submit CSRF enforcement.
# webhook_bp (HMAC-verified external callers), auth_bp (own form tokens) and
# mobile_bp (bearer-token auth, no cookie jar to carry a CSRF cookie — see
# mobile_api.py's module docstring) are intentionally exempt. status_bp is
# protected: its public pages are GETs, which the check never touches, and
# its /admin/status writes post the public outage banner (SEC-23).
for _bp in (admin_bp, client_bp, social_bp, toast_bp, square_bp, clover_bp, rpower_bp, status_bp):
    csrf_protect(_bp)

app.register_blueprint(admin_bp)
from sales_audit_routes import audit_bp
if not getattr(audit_bp, "_csrf_wired", False):
    csrf_protect(audit_bp)
    audit_bp._csrf_wired = True
app.register_blueprint(audit_bp)
app.register_blueprint(webhook_bp)
app.register_blueprint(social_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(client_bp)
app.register_blueprint(toast_bp)
app.register_blueprint(square_bp)
app.register_blueprint(clover_bp)
app.register_blueprint(rpower_bp)
app.register_blueprint(status_bp)
app.register_blueprint(mobile_bp)

# Issues, goals, outcomes, menu, invoices, demand, loss, morning brief — web
# half is CSRF-protected like client_bp; the mobile half is bearer-token like
# mobile_bp; the /i/<token> issue page is a public link whose token is the
# credential (no session, no cookie jar to double-submit from).
from strategy_routes import strategy_bp, strategy_mobile_bp, issue_link_bp
if not getattr(strategy_bp, "_csrf_wired", False):
    csrf_protect(strategy_bp)
    strategy_bp._csrf_wired = True
app.register_blueprint(strategy_bp)
app.register_blueprint(strategy_mobile_bp)
app.register_blueprint(issue_link_bp)

# The staff portal. Deliberately NOT in the csrf_protect tuple above: its
# sign-in POST happens before any session exists (there is no cookie jar to
# double-submit from yet), and every authenticated call it makes is a JSON
# fetch carrying its own httponly session cookie with SameSite=Lax, which is
# the same posture mobile_bp already takes.
from staff_routes import staff_bp
app.register_blueprint(staff_bp)
_check_duplicate_routes()

app.after_request(ensure_csrf_cookie)


@app.teardown_appcontext
def _sweep_leaked_db_connections(exc):
    """Close any SQLite connection this request opened and didn't.

    The prevailing shape in this codebase is conn = get_conn() ... conn.close(),
    which closes on every normal path but not when something raises in between
    — and the traceback keeps the frame, and the connection, alive well past
    the failure. A leaked connection that was mid-write holds a RESERVED lock
    and every other writer waits out the 30-second busy timeout behind it.
    See models.close_thread_connections.
    """
    try:
        import models as _m
        leaked = _m.close_thread_connections()
        if leaked:
            print(f"[db] closed {leaked} leaked connection(s) after {request.path}")
    except Exception:
        pass

_secret_key = os.getenv("SECRET_KEY", "")
if not _secret_key:
    _secret_key = os.urandom(32).hex()
    # Every module that signs something (2FA pending tokens, connect state,
    # reset codes) reads SECRET_KEY; one per-process key for all of them.
    os.environ["SECRET_KEY"] = _secret_key
    print("WARNING: SECRET_KEY not set — sessions will invalidate on every restart. Set SECRET_KEY in Railway env vars.")
app.secret_key = _secret_key

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "will")

RESEND_API_KEY          = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL              = config.from_email()
STRIPE_WEBHOOK_SECRET   = os.getenv("STRIPE_WEBHOOK_SECRET", "")
WILL_EMAIL              = config.will_email()

# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_current_user():
    token = request.cookies.get("session_token")
    return get_session_user(token) if token else None

def login_required(f):
    """Local, weaker copy of auth.login_required — no billing or module
    gating — that guards the dashboard HTML shell at "/".

    It still has to refuse a staff PIN identity: serving them the shell would
    render the whole owner dashboard, and while every XHR it fires would 403
    on its own, the page itself leaks the restaurant's name, module layout
    and navigation. Employees get sent to their own portal instead.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            # The query survives sign-in: an emailed or texted link's ?ask=
            # (the question it asks) and ?rec=/&src= (the recommendation it
            # came from) were dropped here, so a signed-out owner who tapped
            # one landed on a bare Home. safe_next_url still holds the
            # redirect to a path on this site.
            nxt = request.full_path if request.query_string else request.path
            return redirect(url_for("auth.login", next=nxt))
        from auth import _console_denied as _cd_shell
        if _cd_shell(user):
            return redirect(url_for("staff.portal_home"))
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


# Prices live in pricing.py (single copy); create_stripe_checkout in emails.py
# reads them from there. A stale TIER_PRICES dict with the launch prices sat
# here unreferenced for four months.
@app.route("/sitemap.xml")
def sitemap():
    from flask import Response
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://cavnar.ai/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>
  <url><loc>https://cavnar.ai/pricing</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
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
    # A link from an email or a text that names a recommendation (rec=,
    # src=) is an "opened" on it (#32).
    import rec_delivery
    rec_delivery.record_link_open(current_user, request.args)
    from labor import analyse_shifts_for_restaurant
    from marketing import CONTENT_TYPES
    rid     = current_user["restaurant_id"]
    rfilter = request.args.get("filter","all")
    rsearch = request.args.get("search","")
    restaurant = get_restaurant(rid)
    rstats     = get_review_stats(rid)
    # First page only. This used to select and render EVERY review the
    # restaurant had ever received — each one a full card with a draft box,
    # a textarea and a template picker — so the document grew without bound
    # with the review history. The rest load from /api/reviews/page.
    from models import REVIEWS_PAGE_SIZE
    reviews, reviews_total = get_reviews_data(rid, rfilter, rsearch,
                                              limit=REVIEWS_PAGE_SIZE, include_total=True)
    top_issues        = get_top_issues(rid, days=90)
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
                 "dow_summary":{},"potential_savings":0,"potential_savings_weekly":0,"potential_savings_monthly":0,"period_days":0,"labor_target":30.0,
                 "by_day":{},"employee_hours":{},"role_summary":{},"role_summary_sorted":[],"role_max_pct":0,"trend_delta":None,"staff_constraints":{}}
    # Whether this identity may see the restaurant's margins at all. The whole
    # payload below used to be computed and rendered into the page for every
    # session that could load "/", regardless of role or entitlement:
    # _module_permission_denied is path-prefix driven and "/" matches no
    # prefix. ROLE_MANAGER exists specifically to withhold food cost — see the
    # comment on permissions.ROLE_MANAGER — and received it anyway, in the
    # HTML of the first page it loaded, as did restaurants without the module.
    from permissions import FOOD_COST_VIEW as _FC_VIEW, has_permission as _hp_fc
    _can_see_food_cost = bool(restaurant and restaurant.module_inventory) and _hp_fc(current_user, _FC_VIEW)
    inv = {}
    try:
        if not _can_see_food_cost:
            raise _FoodCostWithheld()
        from inventory import analysis_for as _analysis_for_dash
        _inv_items, _inv_live, inv = _analysis_for_dash(rid)
        inv['banner_gradient'] = inv_banner_gradient(inv['annual_waste_projection'], inv['annual_recoverable'])
        try:
            from inventory import compute_item_trends as _cit_dash, build_price_watch as _bpw_dash
            inv['price_watch'] = _bpw_dash(_cit_dash(rid, _inv_items))
        except Exception as _pw_e:
            print(f"Price watch error: {_pw_e}")
            inv['price_watch'] = []
    except _FoodCostWithheld:
        inv = {"withheld": True, "is_live": False, "price_watch": [],
               "waste_items": [], "overstock": [], "critical_low": [],
               "reorder_soon": [], "order_reduction": [], "total_items": 0}
    except Exception as e:
        print(f"Inventory analysis error: {e}")
        # `failed` so the template can say the analysis didn't run. The zeros
        # below are placeholders to keep the page rendering, not measurements
        # — without the flag an owner saw "$0 waste this week" and had no way
        # to know nothing had been computed.
        inv = {"failed":True,"total_waste_cost_week":0,"monthly_waste_projection":0,
               "recoverable_monthly":0,"total_stock_value":0,
               "waste_items":[],"overstock":[],"critical_low":[],
               "reorder_soon":[],"order_reduction":[],"total_items":0,
               "annual_waste_projection":0,"annual_recoverable":0,"waste_rate_pct":0,"benchmark_label":"—","benchmark_tone":"neutral","benchmark_detail":"Food cost analysis is unavailable right now",
               "week_start":"—","week_end":"—","last_updated":"—",
               "banner_gradient":"linear-gradient(to right,#2a0808 0%,#0d331f 100%)",
               "is_live":False,"price_watch":[]}
    # Show welcome banner if user has never logged in before (last_login is None)
    from auth import get_user_by_id
    _user_row = get_user_by_id(current_user["id"]) if not current_user.get("is_admin") else None
    show_welcome = False   # welcome modal retired with the rest of onboarding

    # Getting-started checklist: real completion state per step, shown until
    # everything's done or the owner dismisses it. Answers "what do I do
    # first?" in the app instead of leaving it to the onboarding emails.
    onboarding_steps = []   # onboarding UI retired (Sep 2026) — no checklist on web or iOS
    # Load competitor intel if available
    competitor_data = None
    if restaurant and restaurant.google_place_id and restaurant.competitor_intel and is_full_tier(restaurant):
        import json as _json
        try:
            competitor_data = _json.loads(restaurant.competitor_intel)
        except Exception:
            competitor_data = None
    # The Intel header's market average and standing, from the one definition
    # the phone and the welcome email use (competitor_intel_format; fix I10):
    # the template took a flat mean against the imported-review average.
    intel_market = None
    if competitor_data:
        try:
            import competitor_intel_format as _cif
            from models import get_review_stats as _grs_m
            _sample_m = {} if getattr(restaurant, "gbp_rating", None) else (_grs_m(rid) or {})
            _own_m = _cif.own_rating(getattr(restaurant, "gbp_rating", None),
                                     getattr(restaurant, "gbp_review_count", None),
                                     _sample_m.get("avg_rating"), _sample_m.get("total"))
            _mk_m = _cif.market_rating(competitor_data.get("competitors") or [])
            intel_market = {**_own_m, **_mk_m, **_cif.market_standing(_own_m, _mk_m)}
        except Exception as _mx:
            print(f"[intel market] {_mx}")
            intel_market = None

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
        # Agency equivalent: $1,500/mo social media manager baseline — only count when content has been generated
        _mkt_agency_value = _months_active * 1500 if (int(restaurant.module_marketing or 0) and _mkt_gen > 0) else 0
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

    # ── Value: measured, avoided, available ───────────────────────────────────
    # This block used to compute the banner's "Total Value Delivered" inline,
    # and value_delivered.py carried a second copy of the same arithmetic by
    # explicit admission. Both were wrong in the same way: labor and food
    # cost contributed their GAP TO TARGET — money the restaurant was still
    # losing — as money delivered, so the figure fell when an owner fixed
    # their scheduling and peaked for the worst-run restaurant on the
    # platform. See value_delivered.py's docstring for the full account.
    #
    # There is now ONE implementation, and the three figures it returns are
    # never summed into each other.
    _mod_l = int(restaurant.module_labor or 0)
    try:
        from value_delivered import breakdown as _value_breakdown
        value = _value_breakdown(rid)
    except Exception as _ve:
        print(f"[value] breakdown unavailable: {_ve}")
        value = {"delivered": {"monthly": 0, "wins": 0, "evaluated": 0, "in_flight": 0,
                               "by_module": {}, "biggest": None, "caveat": ""},
                 "avoided": {"items": [], "dollars": 0, "hours": 0},
                 "opportunity": {"items": [], "monthly": 0}}

    # Labor-tab context figures. These are NOT value delivered and never were
    # — they stay here because the Labor tab renders them as context beside
    # the labor percentage, which is an honest use of them.
    _labor_monthly = int(round(labor.get("potential_savings_monthly", 0) or 0))
    _period_days = labor.get("period_days") or labor.get("date_range", {}).get("days") or 0
    _monthly_sales_est = (labor.get("total_sales", 0) / _period_days * 30) if _period_days else 0
    # One benchmark and one set of guards for web and iOS (thresholds.
    # labor_vs_industry_monthly): the web used 32% with no guards while iOS
    # used 34.5% and refused on estimated hours, missing sales or a sub-week
    # period, so the same tile showed two dollar figures (CA1 L33).
    import thresholds as _thr
    _labor_vs_industry_monthly = _thr.labor_vs_industry_monthly(
        labor.get("overall_labor_pct"), labor.get("total_sales"), _period_days,
        hours_are_estimated=bool(labor.get("hours_are_estimated")),
        sales_data_missing=bool(labor.get("sales_data_missing")),
        analysis_failed=bool(labor.get("analysis_failed")))
    _labor_vs_industry_annual  = _labor_vs_industry_monthly * 12
    _inv_value = int(inv.get("recoverable_monthly", 0)) if inv.get("is_live") else 0
    # The "3.1% sales lift from responding to reviews" figure had no source in
    # the product and was worded as the owner's own number (CA4 F5, CA1 R16):
    # retired. The key stays at 0 because dashboard.html still reads it
    # (gSalesLiftYr); that reader and the unreachable .review-rate-count line
    # are a candidate for future cleanup after additional verification.
    _sales_lift_yr = 0
    import value_delivered as _vd_rates
    savings_breakdown = {
        # The banner: measured monthly dollars only.
        "total":            int(round((value["delivered"] or {}).get("monthly") or 0)),
        "wins":             (value["delivered"] or {}).get("wins") or 0,
        "in_flight":        (value["delivered"] or {}).get("in_flight") or 0,
        "avoided_dollars":  int(round((value["avoided"] or {}).get("dollars") or 0)),
        "avoided_hours":    (value["avoided"] or {}).get("hours") or 0,
        # The Reviews tab shows its own cost-avoidance line. Read from the
        # same place as everything else so the rate has one definition.
        # next() needs its default OUTSIDE the generator's parentheses: with
        # `next((x for ...), 0)` mis-bracketed as `round(next(x for ...), 0)`,
        # a restaurant with no responded review raised StopIteration here and
        # the whole dashboard shell was a 500 — every new account, both demo
        # accounts as seeded. Since f41e2e5; found by the Sep 21 re-audit.
        "reviews_avoided":  int(round(next(((i["dollars"] or 0)
                                            for i in (value["avoided"] or {}).get("items") or []
                                            if i.get("key") == "replies"), 0))),
        # The stated rate behind that line, from its one definition, so the
        # line's own caption states it from here too (rec-ROI #12).
        "reply_rate":       _vd_rates.REPLY_RATE,
        "reply_rate_basis": _vd_rates.REPLY_RATE_BASIS,
        "opportunity":      int(round((value["opportunity"] or {}).get("monthly") or 0)),
        # Labor-tab context, unchanged.
        "labor_monthly":    _labor_monthly if _mod_l else 0,
        "inv_monthly":      _inv_value     if int(restaurant.module_inventory or 0) else 0,
        "labor_annual":     int(_labor_monthly * 12) if _mod_l else 0,
        "inv_annual":       int(_inv_value * 12)     if int(restaurant.module_inventory or 0) else 0,
        "labor_overtime":   labor_overtime_cost       if _mod_l else 0,
        "labor_vs_industry_monthly": _labor_vs_industry_monthly if _mod_l else 0,
        "labor_vs_industry_annual":  _labor_vs_industry_annual  if _mod_l else 0,
        "labor_industry_pct":        _thr.LABOR_INDUSTRY_PCT,
        "labor_industry_basis":      _thr.LABOR_INDUSTRY_BASIS,
        "sales_lift_yr":             _sales_lift_yr,
    }

    import secrets as _sec
    csrf_token = request.cookies.get('csrf_token') or _sec.token_hex(16)
    # Labor: upcoming holidays within 21 days for the banner — one list for
    # web and iOS (demand.upcoming_holidays): the date M/D/YY, and a label
    # that states only what THIS restaurant's own sales showed on that
    # holiday last year, else the generic "Holiday — check your own history"
    # (CA1 L30: the banner claimed "your busiest Sundays follow this
    # pattern" for every restaurant).
    _labor_upcoming = []
    try:
        import demand as _demand_hol
        _labor_upcoming = _demand_hol.upcoming_holidays(rid)
    except Exception:
        _labor_upcoming = []

    # Food cost: load saved quick-count data for price drift display
    _food_cost_data = None
    try:
        if _can_see_food_cost:
            from models import get_client_data as _gcd_fc
            import json as _json_fc
            _fc_raw = _gcd_fc(rid)
            if _fc_raw and _fc_raw.get("food_cost_json"):
                _food_cost_data = _json_fc.loads(_fc_raw["food_cost_json"])
    except Exception:
        pass
    # With a live pantry and no price submission yet, the price monitor opens
    # on the pantry itself — name, unit, unit cost, a week's usage — instead
    # of seven sample rows at 0.00 sitting beside the real ingredient list.
    try:
        if _can_see_food_cost and not (_food_cost_data and (_food_cost_data.get("current") or {}).get("items")):
            import inventory_ledger as _il_fc
            _pantry = [r for r in (_il_fc.list_ingredients(rid) or []) if r.get("name")]
            if _pantry:
                _food_cost_data = dict(_food_cost_data or {})
                _food_cost_data["current"] = {
                    "from_pantry": True,
                    "items": [{"name": r["name"], "unit": r.get("unit") or "",
                               "price": (round(float(r["unit_cost"]), 2) if r.get("unit_cost") else ""),
                               "usage": (round(float(r["avg_daily_usage"]) * 7, 1) if r.get("avg_daily_usage") else "")}
                              for r in _pantry]}
    except Exception:
        pass

    # Multi-location: load group locations for owner switcher
    _group_locations = []
    from permissions import LOCATION_SWITCH as _LOC_SW, has_permission as _hp_loc
    if _hp_loc(current_user, _LOC_SW):
        try:
            from models import get_restaurant as _gr_grp, get_location_group as _glg
            _base = _gr_grp(current_user["base_restaurant_id"])
            if _base and _base.location_group:
                _grp = _glg(_base.location_group, owner_email=_base.owner_email)
                _active_id = current_user["restaurant_id"]
                _group_locations = [{"id": r["id"],
                                     "name": r.get("location_name") or r["name"],
                                     "active": r["id"] == _active_id} for r in _grp]
        except Exception:
            pass

    import time_utils as _tu
    from notify import labor_target_for as _labor_target_for
    _today_mdy = _tu.mdy(_tu.restaurant_now(restaurant))
    return render_template('dashboard.html',
        show_welcome=show_welcome,
        onboarding_steps=onboarding_steps,
        csrf_token=csrf_token,
        current_user=current_user, restaurant=restaurant,
        group_locations=_group_locations,
        rstats=rstats, reviews=reviews, reviews_total=reviews_total,
        reviews_page_size=REVIEWS_PAGE_SIZE,
        rfilter=rfilter, rsearch=rsearch, top_issues=top_issues, sentiment_trend=sentiment_trend,
        labor=labor, inv=inv, ctypes=CONTENT_TYPES,
        mod_reviews=int(restaurant.module_reviews or 0),
        mod_labor=int(restaurant.module_labor or 0),
        # Entitlement AND permission. The tab button was gated on the module
        # alone; the panel itself was gated on nothing.
        mod_inventory=int(1 if _can_see_food_cost else 0),
        mod_marketing=int(restaurant.module_marketing or 0),
        is_full_tier=is_full_tier(restaurant),
        # The registry-backed single source of truth (models.get_active_modules) —
        # not yet consumed by dashboard.html's many independent {% if mod_x %}
        # checks (a larger, separate template refactor), but exposed here so the
        # web layer exercises the same function the mobile API now relies on,
        # rather than that function only ever running for mobile requests.
        active_modules=get_active_modules(restaurant),
        # The Home kicker's first paint: M/D/YY, on the restaurant's own date
        # (the server's clock is UTC on Railway, so after 7pm in Chicago it
        # already read tomorrow) — CLAUDE.md's dates rule.
        now=_today_mdy,
        now_mdy=_today_mdy,
        viewing_as=current_user.get("is_admin", 0),
        labor_target=_labor_target_for(restaurant),
        labor_overtime_cost=labor_overtime_cost,
        mkt_stats=mkt_stats,
        savings_breakdown=savings_breakdown,
        competitor_data=competitor_data,
        intel_market=intel_market,
        competitor_updated_at=restaurant.competitor_updated_at if restaurant else None,
        labor_upcoming=_labor_upcoming,
        food_cost_data=_food_cost_data)

def _json_api_path():
    """/api/* and /mobile/api/* callers are fetch() and the iOS app: they
    parse JSON and have no use for an HTML error page. The web dashboard's
    r.json() threw on one, so a 404, 405, 413 or 500 read as "network
    failure" instead of the server's reason (CLIENT-12)."""
    path = request.path or ""
    return path.startswith("/api/") or path.startswith("/mobile/api/")


@app.errorhandler(405)
def method_not_allowed(e):
    if _json_api_path():
        return jsonify(ok=False, error="That request isn't supported here — refresh the page and try again."), 405
    return e


@app.errorhandler(413)
def payload_too_large(e):
    if _json_api_path():
        return jsonify(ok=False, error="That's too large to upload — the limit is 5 MB."), 413
    return e


@app.errorhandler(403)
def forbidden(e):
    """A bare abort(403) anywhere in the app (e.g. status_routes._require_admin)
    otherwise falls through to Flask's default HTML error page, which breaks
    JSON-only callers — the iOS app in particular has no HTML to parse and
    just shows a generic "Something went wrong (403)" with no real reason.
    Mobile routes always get real JSON; other routes keep an HTML page."""
    from flask import Response
    if _json_api_path():
        return jsonify(ok=False, error="You don't have permission to do that."), 403
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Access Denied — Cavnar AI</title>
  <link rel="stylesheet" href="/static/fonts/cavnar-fonts.css">
  <style>
    body{margin:0;background:#f7f4ef;font-family:'Apfel Grotezk',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
    .wrap{max-width:420px;padding:40px 24px}
    .logo{font-family:'Clash Display',sans-serif;font-size:28px;color:#0e0c0a;margin-bottom:32px}
    
    h1{font-family:'Clash Display',sans-serif;font-size:64px;color:#0e0c0a;margin:0 0 8px;line-height:1}
    p{font-size:15px;color:#7a736a;line-height:1.6;margin:0 0 24px}
    a.btn{display:inline-block;background:#c84b2f;color:white;padding:10px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="logo"><svg viewBox="-6 -14 666 128" height="0.8em" style="vertical-align:-0.08em" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M53.43 101.49Q28.81 101.49 14.40 87.46Q0.00 73.43 0.00 50.00Q0.00 26.57 14.40 12.54Q28.81 -1.49 53.43 -1.49Q76.87 -1.49 90.67 9.48Q104.48 20.45 104.48 39.40V41.64H79.55V39.40Q79.55 29.40 73.43 24.70Q67.31 20.00 53.88 20.00Q37.31 20.00 30.52 26.72Q23.73 33.43 23.73 50.00Q23.73 66.57 30.52 73.28Q37.31 80.00 53.88 80.00Q67.31 80.00 73.43 75.30Q79.55 70.60 79.55 60.60V58.36H104.48V60.60Q104.48 79.55 90.67 90.52Q76.87 101.49 53.43 101.49Z"/><path d="M127.87 100.00H102.79L146.52 0.00H178.47L222.50 100.00H196.82L187.42 77.91H137.42ZM155.48 35.97 146.08 57.76H178.76L169.36 35.97L163.24 20.45H161.60Z"/><path d="M266.08 100.00H234.14L192.65 0.00H219.67L249.67 77.31H251.16L280.71 0.00H307.58Z"/><path d="M341.47 100.00H319.08V0.00H342.96L378.78 47.31L392.51 68.36H394.15L393.11 48.21V0.00H415.50V100.00H391.61L354.75 52.24L342.06 33.43H340.57L341.47 51.79Z"/><path d="M451.92 100.00H426.85L470.58 0.00H502.52L546.55 100.00H520.88L511.47 77.91H461.47ZM479.53 35.97 470.13 57.76H502.82L493.41 35.97L487.29 20.45H485.65Z"/><path d="M580.39 100.00H558.00V0.00H613.22Q631.73 0.00 641.80 7.84Q651.88 15.67 651.88 30.00Q651.88 55.07 623.67 57.76V58.96Q629.94 60.60 633.30 63.88Q636.65 67.16 639.79 73.13L654.27 100.00H628.30L614.56 74.03Q611.43 68.06 607.55 65.90Q603.67 63.73 595.16 63.73H580.39ZM580.39 20.15V46.87H613.07Q621.28 46.87 624.86 43.96Q628.45 41.04 628.45 33.43Q628.45 26.12 624.79 23.13Q621.13 20.15 613.07 20.15Z"/><circle cx="250.1" cy="28" r="11" fill="#D4583A"/></svg> <span style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;font-weight:800;font-size:0.34em;letter-spacing:0.16em;color:#D4583A;vertical-align:0.62em;margin-left:0.12em;font-style:normal">AI</span></div>
    <h1>403</h1>
    <p>You don't have permission to view this page.</p>
    <a href="/login" class="btn">Back to dashboard</a>
  </div>
</body>
</html>"""
    return Response(html, status=403, mimetype="text/html")

@app.errorhandler(413)
def request_too_large(e):
    """A body over MAX_CONTENT_LENGTH. Flask's default is an HTML page, which
    the web's r.json() and the iOS decoder both dead-end on as "Upload
    failed" with no reason (MOD-MKT-14). JSON for the APIs, like 403/404."""
    from flask import Response
    limit_mb = (app.config.get("MAX_CONTENT_LENGTH") or 0) / (1024 * 1024)
    msg = (f"That upload is too large. Keep it under {limit_mb:g} MB — "
           "for a photo, pick a smaller one or take a screenshot of it.")
    if request.path.startswith(("/api/", "/mobile/api/")):
        return jsonify(ok=False, error=msg), 413
    return Response(msg, status=413, mimetype="text/plain")


@app.errorhandler(404)
def page_not_found(e):
    from flask import Response
    if request.path.startswith("/mobile/api/"):
        return jsonify(ok=False, error="That endpoint doesn't exist. Please update the app."), 404
    if _json_api_path():
        return jsonify(ok=False, error="That endpoint doesn't exist — refresh the page to load the latest version."), 404
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Page Not Found — Cavnar AI</title>
  <link rel="stylesheet" href="/static/fonts/cavnar-fonts.css">
  <style>
    body{margin:0;background:#f7f4ef;font-family:'Apfel Grotezk',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
    .wrap{max-width:420px;padding:40px 24px}
    .logo{font-family:'Clash Display',sans-serif;font-size:28px;color:#0e0c0a;margin-bottom:32px}
    
    h1{font-family:'Clash Display',sans-serif;font-size:64px;color:#0e0c0a;margin:0 0 8px;line-height:1}
    p{font-size:15px;color:#7a736a;line-height:1.6;margin:0 0 24px}
    a.btn{display:inline-block;background:#c84b2f;color:white;padding:10px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="logo"><svg viewBox="-6 -14 666 128" height="0.8em" style="vertical-align:-0.08em" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M53.43 101.49Q28.81 101.49 14.40 87.46Q0.00 73.43 0.00 50.00Q0.00 26.57 14.40 12.54Q28.81 -1.49 53.43 -1.49Q76.87 -1.49 90.67 9.48Q104.48 20.45 104.48 39.40V41.64H79.55V39.40Q79.55 29.40 73.43 24.70Q67.31 20.00 53.88 20.00Q37.31 20.00 30.52 26.72Q23.73 33.43 23.73 50.00Q23.73 66.57 30.52 73.28Q37.31 80.00 53.88 80.00Q67.31 80.00 73.43 75.30Q79.55 70.60 79.55 60.60V58.36H104.48V60.60Q104.48 79.55 90.67 90.52Q76.87 101.49 53.43 101.49Z"/><path d="M127.87 100.00H102.79L146.52 0.00H178.47L222.50 100.00H196.82L187.42 77.91H137.42ZM155.48 35.97 146.08 57.76H178.76L169.36 35.97L163.24 20.45H161.60Z"/><path d="M266.08 100.00H234.14L192.65 0.00H219.67L249.67 77.31H251.16L280.71 0.00H307.58Z"/><path d="M341.47 100.00H319.08V0.00H342.96L378.78 47.31L392.51 68.36H394.15L393.11 48.21V0.00H415.50V100.00H391.61L354.75 52.24L342.06 33.43H340.57L341.47 51.79Z"/><path d="M451.92 100.00H426.85L470.58 0.00H502.52L546.55 100.00H520.88L511.47 77.91H461.47ZM479.53 35.97 470.13 57.76H502.82L493.41 35.97L487.29 20.45H485.65Z"/><path d="M580.39 100.00H558.00V0.00H613.22Q631.73 0.00 641.80 7.84Q651.88 15.67 651.88 30.00Q651.88 55.07 623.67 57.76V58.96Q629.94 60.60 633.30 63.88Q636.65 67.16 639.79 73.13L654.27 100.00H628.30L614.56 74.03Q611.43 68.06 607.55 65.90Q603.67 63.73 595.16 63.73H580.39ZM580.39 20.15V46.87H613.07Q621.28 46.87 624.86 43.96Q628.45 41.04 628.45 33.43Q628.45 26.12 624.79 23.13Q621.13 20.15 613.07 20.15Z"/><circle cx="250.1" cy="28" r="11" fill="#D4583A"/></svg> <span style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;font-weight:800;font-size:0.34em;letter-spacing:0.16em;color:#D4583A;vertical-align:0.62em;margin-left:0.12em;font-style:normal">AI</span></div>
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
    if _json_api_path():
        return jsonify(ok=False, error="Something went wrong on our end. It's been logged — please try again."), 500
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Something went wrong — Cavnar AI</title>
  <link rel="stylesheet" href="/static/fonts/cavnar-fonts.css">
  <style>
    body{margin:0;background:#f7f4ef;font-family:'Apfel Grotezk',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
    .wrap{max-width:420px;padding:40px 24px}
    .logo{font-family:'Clash Display',sans-serif;font-size:28px;color:#0e0c0a;margin-bottom:32px}
    
    h1{font-family:'Clash Display',sans-serif;font-size:40px;color:#0e0c0a;margin:0 0 8px}
    p{font-size:15px;color:#7a736a;line-height:1.6;margin:0 0 24px}
    a.btn{display:inline-block;background:#c84b2f;color:white;padding:10px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="logo"><svg viewBox="-6 -14 666 128" height="0.8em" style="vertical-align:-0.08em" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M53.43 101.49Q28.81 101.49 14.40 87.46Q0.00 73.43 0.00 50.00Q0.00 26.57 14.40 12.54Q28.81 -1.49 53.43 -1.49Q76.87 -1.49 90.67 9.48Q104.48 20.45 104.48 39.40V41.64H79.55V39.40Q79.55 29.40 73.43 24.70Q67.31 20.00 53.88 20.00Q37.31 20.00 30.52 26.72Q23.73 33.43 23.73 50.00Q23.73 66.57 30.52 73.28Q37.31 80.00 53.88 80.00Q67.31 80.00 73.43 75.30Q79.55 70.60 79.55 60.60V58.36H104.48V60.60Q104.48 79.55 90.67 90.52Q76.87 101.49 53.43 101.49Z"/><path d="M127.87 100.00H102.79L146.52 0.00H178.47L222.50 100.00H196.82L187.42 77.91H137.42ZM155.48 35.97 146.08 57.76H178.76L169.36 35.97L163.24 20.45H161.60Z"/><path d="M266.08 100.00H234.14L192.65 0.00H219.67L249.67 77.31H251.16L280.71 0.00H307.58Z"/><path d="M341.47 100.00H319.08V0.00H342.96L378.78 47.31L392.51 68.36H394.15L393.11 48.21V0.00H415.50V100.00H391.61L354.75 52.24L342.06 33.43H340.57L341.47 51.79Z"/><path d="M451.92 100.00H426.85L470.58 0.00H502.52L546.55 100.00H520.88L511.47 77.91H461.47ZM479.53 35.97 470.13 57.76H502.82L493.41 35.97L487.29 20.45H485.65Z"/><path d="M580.39 100.00H558.00V0.00H613.22Q631.73 0.00 641.80 7.84Q651.88 15.67 651.88 30.00Q651.88 55.07 623.67 57.76V58.96Q629.94 60.60 633.30 63.88Q636.65 67.16 639.79 73.13L654.27 100.00H628.30L614.56 74.03Q611.43 68.06 607.55 65.90Q603.67 63.73 595.16 63.73H580.39ZM580.39 20.15V46.87H613.07Q621.28 46.87 624.86 43.96Q628.45 41.04 628.45 33.43Q628.45 26.12 624.79 23.13Q621.13 20.15 613.07 20.15Z"/><circle cx="250.1" cy="28" r="11" fill="#D4583A"/></svg> <span style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;font-weight:800;font-size:0.34em;letter-spacing:0.16em;color:#D4583A;vertical-align:0.62em;margin-left:0.12em;font-style:normal">AI</span></div>
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
    from models import init_staff_notes as _isn, init_staff_availability as _isa
    from models import init_two_fa_backup_codes as _i2fabc
    from models import init_competitor_snapshots as _ics
    from models import init_ai_visibility_queries as _iavq
    from models import init_staff_capabilities as _isc
    from models import init_shift_profiles as _isp
    from models import init_capability_changes as _icc
    from models import init_ask_memory as _iam
    from auth import init_auth as _init_auth
    from webhooks import init_webhooks as _iwh
    from guest_marketing import init_guest_marketing as _igm
    from push import init_push as _ipush
    # A requested restore swaps the snapshot in before the first connection
    # (db_restore.py). A refused one raises, so the deploy fails loudly.
    import db_restore as _dbr
    _dbr.restore_if_requested()
    _init_db()
    _init_auth()
    # The public status page's service rows. Seeded here, once, rather than
    # by /status on every public GET (SEC-38); the scheduler's health check
    # also re-seeds, so a service added to SERVICES appears without a deploy.
    from status_manager import seed_default_services as _seed_status
    _seed_status()
    _isn()
    _isa()
    _ec()
    _iel()
    _ioe()
    _iwh()
    _igm()
    _ipush()
    _i2fabc()
    _ics()
    _iavq()
    _isc()
    _isp()
    _icc()
    _iam()
    # A previous process may have been killed mid-generation, leaving a job
    # pending forever and a client polling an answer that will never come.
    try:
        import ops as _ops_boot
        _ops_boot.sweep_stale_jobs()
    except Exception:
        pass
    from sales_audits import init_sales_audits as _isa2, ensure_first_audit as _efa
    _isa2()
    _efa()
    print("DB init OK")
except Exception as _e:
    print(f"DB init error: {_e}")
    # Fail the boot. Carrying on served every request on a half-migrated
    # schema and let a refused restore (above) promote as healthy; a failed
    # deploy instead leaves the previous container serving (DATA-11).
    raise

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
        if _admin_pw == "changeme123":
            print("SECURITY WARNING: ADMIN_PASSWORD is not set — using insecure default. Set ADMIN_PASSWORD in Railway env vars immediately.")
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

# The scheduler runs in this process by default, which is how it has always
# worked and what a single-service deployment needs.
#
# Set RUN_SCHEDULER_IN_WEB=0 only when worker.py runs as a second process in
# THIS SAME Railway service (see docs/ops/RAILWAY_SCHEDULER_SPLIT.md). Never as a
# separate service: Railway volumes cannot be shared between services, so a
# separate worker would schedule against an empty database of its own while
# this process, told not to schedule, left every real job unrun.
#
# Running BOTH is safe and is the intended migration path — ops.acquire_
# scheduler_lease() elects exactly one runner across processes, so the loser
# idles and takes over only if the holder stops heartbeating. Unsetting the
# variable is therefore also the rollback.
_RUN_SCHEDULER_IN_WEB = os.getenv("RUN_SCHEDULER_IN_WEB", "1").strip().lower() not in ("0", "false", "no")
if _RUN_SCHEDULER_IN_WEB:
    try:
        from scheduler import start_scheduler as _ss
        # Returns None off Railway: see scheduler.scheduling_allowed.
        if _ss() is not None:
            print("Scheduler started OK")
    except Exception as _e:
        print(f"Scheduler start error: {_e}")
else:
    print("Scheduler not started in web process (RUN_SCHEDULER_IN_WEB=0) — "
          "worker.py is expected to be running it")

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


# The demo accounts are seeded and refreshed off the request path (demo_seed.py).
import demo_seed as _demo_seed
_demo_seed.start_background_seed()

if __name__ == "__main__":
    print(f"\n  Hosted dashboard → http://localhost:{PORT}")
    print(f"  Admin panel      → http://localhost:{PORT}/admin\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
# redeploy Sat Jun  6 16:35:16 CDT 2026
