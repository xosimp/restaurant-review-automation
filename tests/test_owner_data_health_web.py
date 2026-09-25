"""Data Freshness audit (9/24/26), workstream O — the web owner surfaces in
templates/dashboard.html: the Data Health drawer and module badges (#21-23),
the Why? drawer's "N% once <source> is current" (#22), restaurant-scoped AI
read caches wiped on a switch and on Sync now (#14), Account → Connections
and the Reviews tab reading the registry (#17, #34), the Labor and Food Cost
heroes (#33), AI visibility's measured date (#35), Marketing's metrics sync
line (#36) and "current", not "live" (#27). The JS runs under node where it
can; the rest is held against the source."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


# ── the Why? drawer (#22) ───────────────────────────────────────────────────

def _conf_block():
    return re.search(r'<script id="cav-conf">(.*?)</script>', _src(), re.S).group(1)


K1 = {"pct": 49, "band": "low", "label": "49% confidence", "reason": "the data under it is out of date",
      "dimensions": {"evidence": {"pct": 90, "basis": "60 days", "n": 60},
                     "accuracy": {"pct": None, "basis": "Not enough history yet", "n": 0},
                     "freshness": {"pct": 30, "basis": "Counts from 9/2/26", "as_of_iso": "2026-09-02",
                                   "stalest": "inventory"}},
      "version": 1,
      "confidence_impact": {"now": 49, "when_current": 70, "delta": 21, "blocked_by": "inventory"}}


def test_the_why_drawer_says_what_the_figure_reads_once_the_data_is_current():
    small = dict(K1, confidence_impact={"now": 49, "when_current": 51, "delta": 2, "blocked_by": "inventory"})
    out = _node("var window={};\n" + _conf_block() + "\nvar C=window.cavConf;\n"
                "var r=C.rows(C.norm(" + json.dumps(K1) + "));var s=C.rows(C.norm(" + json.dumps(small) + "));"
                "console.log(JSON.stringify({note:r[2].note,small:s[2].note||'',panel:C.panel(C.norm("
                + json.dumps(K1) + "))}));")
    assert out["note"] == "70% once inventory counts are current"
    assert out["small"] == ""
    assert "70%</span> once inventory counts are current" in out["panel"]


# ── the AI-read caches (#14) ────────────────────────────────────────────────

def _cache_block():
    s = _src()
    a = s.index("/* AI-read caches (Data Freshness #14")
    b = s.index("function addCustomFoodItem(){", a)
    return s[a:b]


def test_ai_read_caches_are_keyed_by_restaurant_and_wiped_whole():
    js = r"""
var store={};
var sessionStorage={getItem:function(k){return store.hasOwnProperty(k)?store[k]:null;},
  setItem:function(k,v){store[k]=String(v);},removeItem:function(k){delete store[k];},
  key:function(i){return Object.keys(store)[i];},get length(){return Object.keys(store).length;}};
var rid='7';
var document={querySelector:function(){return {getAttribute:function(){return rid;}};}};
""" + _cache_block() + r"""
cavSet('review_insight','A');
rid='8';
var other=cavGet('review_insight');
cavSet('review_insight','B');
store['review_insight']='legacy';store['_gbpReload']='1';
var keys=Object.keys(store).sort();
cavWipe();
console.log(JSON.stringify({other:other,keys:keys,after:Object.keys(store)}));
"""
    out = _node(js)
    assert out["other"] is None                                     # location 8 never sees 7's read
    assert "cav:review_insight:7" in out["keys"] and "cav:review_insight:8" in out["keys"]
    assert out["after"] == ["_gbpReload"]                           # every cached read gone, nothing else


def test_every_ai_read_cache_goes_through_the_scoped_helpers():
    s = _src()
    for k in ("review_insight", "labor_insight", "inv_insight", "mkt_brief", "aiv_cache"):
        assert not re.search(r"sessionStorage\.(get|set|remove)Item\('" + k, s), k
    sw = s[s.index("window.switchLocation = function(rid"):][:600]
    assert "cavWipe()" in sw
    # Sync now drops them too, and the food entry holds the whole payload.
    assert "cavWipe" in s[s.index("function dhSync(btn)"):][:1500]
    assert "cavSet('inv_insight',JSON.stringify(" in s
    assert s.count("cavReadNote(el,") >= 4


# ── the Data Health drawer and badges (#21-23) ──────────────────────────────

def _dh_block():
    s = _src()
    a = s.index("// ── Data Health (Data Freshness #21-23)")
    b = s.index("function renderFollow(g){", a)
    return s[a:b]


SNAP = {"ok": True, "generated_at": "2026-09-24T14:02:11Z",
        "overall": {"pct": 71, "state": "aging", "label": "71% data health", "reason": "Inventory counts are 12 days old"},
        "sources": [{"key": "pos", "label": "POS sales", "tone": "ok", "pct": 100, "line": "POS sales: Toast sales through 9/23/26",
                     "reliability": {"basis": "6 of the last 7 syncs succeeded"}, "expected_line": "POS sync runs tonight 3am",
                     "can_sync_now": True},
                    {"key": "inventory", "label": "Inventory counts", "tone": "bad", "pct": 30,
                     "line": "Inventory counts: Counts from 9/12/26"}],
        "not_connected": [{"key": "marketing", "label": "Marketing metrics",
                           "next": "Connect Instagram or Facebook in Account → Connections"}],
        "modules": [{"module": "food_cost", "title": "Food cost", "sources": ["inventory", "pos", "sales"],
                     "confidence_impact": {"now": 81, "when_current": 94, "basis": "median of 3 open recommendations",
                                           "line": "Food cost recommendations 81% → 94% once inventory counts are current"}}]}


def test_the_drawer_draws_every_source_the_gaps_the_impact_and_sync_now():
    js = ("var document={readyState:'complete',addEventListener:function(){},querySelector:function(){return null;},"
          "querySelectorAll:function(){return [];}};var window={};"
          "function esc(v){return String(v==null?'':v);}function num(v){return esc(v);}function ago(){return 'just now';}"
          "function hbExplain(){}function apiJson(r){return r;}\n" + _dh_block()
          + "\nconsole.log(JSON.stringify({h:window.cavDataHealth.body(" + json.dumps(SNAP) + ")}));")
    h = _node(js)["h"]
    for want in ("71<small>%</small>", "POS sales: Toast sales through 9/23/26", "6 of the last 7 syncs succeeded",
                 "POS sync runs tonight 3am", "Connect Instagram or Facebook in Account → Connections",
                 "Food cost recommendations 81% → 94% once inventory counts are current",
                 'data-dh-sync="pos"', "a support score, not a grade"):
        assert want in h, want
    assert 'class="cbtn cbtn-primary cbtn-sm" data-dh-sync' in h


def test_home_kicker_is_the_score_and_every_module_header_has_a_badge():
    s = _src()
    rf = s[s.index("function renderFreshness(d){"):][:2200]
    assert "Data health <span class=\"hb-num\">" in rf and 'data-dh-open="1"' in rf and "cbtn" in rf
    for m in ("reviews", "labor", "food_cost", "marketing", "intel"):
        assert f'data-dh-module="{m}"' in s, m
    # "live" is reserved for a source inside its cadence (#27).
    assert "</span> live '+" not in s and "</span> current '+" in s


# ── Connections, Reviews, heroes, visibility, marketing (#17, #33-36) ───────

def test_server_rendered_lines_read_the_registry():
    s = _src()
    assert "Live from Google" not in s
    card = s[s.index("{% macro pos_card("):][:3000]
    assert "</span> UTC" not in card and "reg.line" in card
    assert s.count("conn_lines.pos.provider ==") == 4
    assert "_gl2.line" in s and "_gl.line" in s
    assert "{% elif _lab_stale %}{% set _head = 'Out of date' %}" in s
    assert "labor.date_range.start|format_date" in s
    assert "From your count of" in s and "{% elif _inv_stale %}" in s
    assert "function aivMeasured(d)" in s and "treat it as background" in s[s.index("function aivMeasured(d)"):][:1200]
    assert "d.metrics_sync" in s and 'id="mkt-perf-sync"' in s
    assert "panel-(labor|inventory|account|competitor)" in s


RENDER = r'''
import os, sqlite3, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import hosted_dashboard as h
import auth, models
from models import Restaurant
rid = models.create_restaurant(Restaurant(name="Wired Co", owner_email="w@x.test", module_reviews=1, module_labor=1))
c = models.get_conn()
stale = (datetime.now(ZoneInfo("America/Chicago")) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
c.execute("UPDATE restaurants SET toast_restaurant_guid='g', toast_last_synced=?, gmb_refresh_token='t', "
          "last_fetched_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), stale, rid))
c.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_cost, sales, total_hours) VALUES (?,?,?,?,?)",
          (rid, (datetime.now(ZoneInfo("America/Chicago")).date() - timedelta(days=9)).isoformat(), 900, 4000, 70))
c.commit(); c.close()
uid = auth.create_user(rid, "wired", "w@x.test", "correct-horse-battery", is_admin=False)
t = h.app.test_client()
import re
page = t.get("/login").get_data(as_text=True)
token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
r = t.post("/login", data={"username": "wired", "password": "correct-horse-battery", "csrf_token": token})
r = t.get("/")
body = r.get_data(as_text=True)
print("STATUS", r.status_code)
print("LASTCHECK", "Last check " in body and "checks missed" in body)
print("SALES", "Sales through " in body)
print("UTC", " UTC</span>" in body)
print("LIVE", "Live from Google" in body)
'''


def test_the_dashboard_renders_the_registry_lines_for_a_wired_restaurant():
    vol = tempfile.mkdtemp(prefix="cavnar-dh-")
    out = subprocess.run([sys.executable, "-c", RENDER, vol], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    assert "STATUS 200" in out.stdout
    assert "LASTCHECK True" in out.stdout and "SALES True" in out.stdout
    assert "UTC False" in out.stdout and "LIVE False" in out.stdout
