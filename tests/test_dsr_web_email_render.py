"""The nightly DSR as the web draws it and the email says it (re-audit
9/25/26, D3/D2 web + email items). The web page's DSR script runs under node
on payloads shaped like dsr.access.render's, so each rule is checked on what
the page actually draws: coverage names (not "[object Object]"), the
close-out's source said once, a starting labor target never red, no
all-dash Labor % column for a login without Labor, the manager's Top KPIs
never repeating Operations, Close day expecting the right version, and the
email's location switch. The email: estimates labelled, AI insights,
tomorrow's date, the location on both links."""
import json
import os
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _dsr_script():
    src = open(DASHBOARD, encoding="utf-8").read()
    start = src.index("/* ── The daily report: one night, the week grid, the period")
    end = src.index("})();\n</script>", start)
    body = src[start:end]
    # Expose the page's own functions to the harness (test-only).
    return body + ("window.__t={renderNight:renderNight,weekHtml:weekHtml,closeDay:closeDay,st:st,"
                   "section:section};\n})();\n")


HARNESS = r"""
var calls={switched:[],replaced:[],opened:[]};
var bodyEl={innerHTML:''};
var meta={getAttribute:function(){return String(HERE);}};
var document={hidden:false,getElementById:function(id){return id==='dr-body'?bodyEl:null;},
  querySelector:function(q){return q.indexOf('cavnar-restaurant-id')>=0?meta:null;},
  querySelectorAll:function(){return [];},addEventListener:function(){}};
var window={location:{search:SEARCH,hash:'#dsr/2026-09-19',pathname:'/'},scrollTo:function(){},
  mdy:function(s){var p=String(s).split('-');return (+p[1])+'/'+(+p[2])+'/'+p[0].slice(2);}};
var history={replaceState:function(a,b,u){calls.replaced.push(u);},pushState:function(){}};
window.history=history;
window.cavHistory=function(){};var cavHistory=window.cavHistory;
window.switchLocation=function(rid,onFail){calls.switched.push(rid);};
function apiJson(r){return r;}function loadFailed(){}
var fetch=function(){return Promise.resolve(FETCH);};
function setInterval(){return 1;}function clearInterval(){}
"""


def _node(script, search="", here=3, fetch=None, tail=""):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = (HARNESS.replace("SEARCH", json.dumps(search)).replace("HERE", json.dumps(here))
          .replace("FETCH", json.dumps(fetch or {"ok": False}))
          + _dsr_script() + script + tail)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _payload(view="owner", **over):
    labor = {"status": "ready", "source": "rpower",
             "metrics": {"pct": 31.0, "target_pct": 30.0, "vs_target_pts": 1.0, "cost": 2790.0, "hours": 180.0,
                         "no_shows": 1, "late_arrivals": 1},
             "detail": {"target_source": "default", "target_label": "Cavnar’s starting target",
                        "coverage": {"measured": True,
                                     "no_shows": [{"employee": "Dana Ruiz", "role": "Server", "shift_start": "16:00"}],
                                     "late": [{"employee": "Sam Oh", "role": "Cook", "shift_start": "15:00"}]}}}
    closeout = {"status": "ready", "source": "manager",
                "detail": {"submitted_by": "Mo", "filed_at_label": "11:42pm", "fields": {"notes": "Busy night"},
                           "labels": {"notes": "Notes"}, "order": ["notes"]}}
    p = {"ok": True, "view": view, "business_date": "2026-09-19", "version": 1, "status": "final",
         "versions": [{"version": 1, "status": "final"}],
         "facts": {"blocks": {"labor": labor, "closeout": closeout}, "withheld": []},
         "checklist": {}, "narrative": {"executive_summary": {"text": "A steady Saturday."}}}
    p.update(over)
    return p


def _render(p, **kw):
    return _node("window.__t.renderNight(%s);console.log(bodyEl.innerHTML);" % json.dumps(p), **kw)


# ── D3-3: the no-show names, not [object Object] ────────────────────────────

def test_labor_names_the_no_shows_and_the_late_clock_ins():
    html = _render(_payload())
    assert "[object Object]" not in html
    assert "<b>No-shows:</b> Dana Ruiz" in html
    assert "<b>Late clock-ins:</b> Sam Oh" in html


# ── D3-12: a starting target is never red ───────────────────────────────────

def test_labor_over_a_starting_target_is_amber_and_over_the_owners_is_red():
    html = _render(_payload())
    tile = re.search(r'<div class="hb-stat([^"]*)"><div class="k">Labor</div>', html)
    assert tile and tile.group(1).strip() == "warn"
    assert "Cavnar&#39;s starting target" in html or "Cavnar’s starting target" in html
    p = _payload()
    p["facts"]["blocks"]["labor"]["detail"]["target_source"] = "set"
    p["facts"]["blocks"]["labor"]["detail"]["target_label"] = "your target"
    tile = re.search(r'<div class="hb-stat([^"]*)"><div class="k">Labor</div>', _render(p))
    assert tile and tile.group(1).strip() == "bad"


def test_the_labor_block_says_whose_target_it_is():
    src = open(os.path.join(ROOT, "dsr", "block_labor.py"), encoding="utf-8").read()
    assert '"target_source": target_source' in src and "thresholds.target_for(ctx.restaurant, \"labor\")" in src


# ── D3-11: the close-out ────────────────────────────────────────────────────

def test_the_close_out_has_no_source_pill_and_says_filed_by_once():
    html = _render(_payload())
    sec = html[html.index('id="dr-sec-closeout"'):]
    sec = sec[:sec.index("</details>")]
    assert '<span class="src">' not in sec
    assert sec.count("Filed by") == 1
    assert "11:42pm" in sec and "in their own words" in sec.lower()


# ── D2-11 / D3-5 (web): Operations and Top KPIs never repeat a tile ────────

def test_the_managers_top_kpis_never_repeat_an_operations_tile():
    k = lambda key, label: {"key": key, "label": label, "value_text": "1"}
    p = _payload("manager", operations=[k("guests", "Guests"), k("avg_ticket", "Average ticket")],
                 kpis=[k("guests", "Guests"), k("labor_pct", "Labor")])
    html = _render(p)
    assert html.count('<div class="k">Guests</div>') == 1
    assert '<div class="k">Labor</div>' in html


# ── D3-13 (web): no Labor % column of dashes ────────────────────────────────

def test_the_week_grid_drops_labor_for_a_login_without_it():
    g = {"start": "2026-09-14", "end": "2026-09-20", "days": [{"date": "2026-09-14", "net": 5000}], "totals": {},
         "categories": [], "withheld": ["budget", "labor"]}
    out = _node("console.log(window.__t.weekHtml(%s,'manager'));" % json.dumps(g))
    assert "Labor %" not in out
    g["withheld"] = ["budget"]
    assert "Labor %" in _node("console.log(window.__t.weekHtml(%s,'manager'));" % json.dumps(g))


# ── D3-10: Close day on a night in flight expects the same version ──────────

@pytest.mark.parametrize("status,version,expect", [("collecting", 1, 1), ("awaiting_close", 2, 2),
                                                   ("failed", 1, 2), ("provisional", 1, 2), ("scheduled", None, 1)])
def test_close_day_waits_for_the_version_the_server_will_write(status, version, expect):
    btn = ("{getAttribute:function(k){return k==='data-date'?'2026-09-19':null;},"
           "closest:function(){return null;},disabled:false}")
    out = _node("window.dsrOpen=function(){calls.opened.push(1);};window.__t.closeDay(%s);" % btn,
                fetch={"ok": True, "started": True, "business_date": "2026-09-19", "status": status,
                       "version": version},
                tail="setTimeout(function(){console.log(JSON.stringify(window.__t.st.expect));},20);")
    assert json.loads(out.strip()) == expect


# ── D2-7 (web): the email's link switches to its own location first ─────────

def test_a_report_link_for_another_location_switches_there_first():
    out = _node("window.dsrOpen('night','2026-09-19');console.log(JSON.stringify(calls));",
                search="?rid=7", here=3)
    c = json.loads(out.strip().splitlines()[-1])
    assert c["switched"] == [7]
    assert c["replaced"] and "rid=" not in c["replaced"][0]


def test_a_report_link_for_this_location_just_opens():
    out = _node("window.dsrOpen('night','2026-09-19');console.log(JSON.stringify(calls));",
                search="?rid=3", here=3)
    c = json.loads(out.strip().splitlines()[-1])
    assert c["switched"] == []


def test_switch_location_hands_a_refusal_back_to_its_caller():
    src = open(DASHBOARD, encoding="utf-8").read()
    assert "window.switchLocation = function(rid, onFail)" in src
    assert "if (onFail) onFail(d);" in src and "if (onFail) onFail(null);" in src


# ── the email ───────────────────────────────────────────────────────────────

def _digest(view="owner", **over):
    d = {"name": "Simple EJ's", "business_date": "2026-09-19", "date_short": "Sat 9/19/26",
         "date_long": "Saturday 9/19/26", "weekday": "Saturday", "fiscal": None, "view": view, "kind": "first",
         "provisional": False, "version": 1, "net": 9000.0, "net_label": "$9,000", "compare": None, "stats": [],
         "lead": "A steady Saturday.", "lead_missing": None, "went_well": [], "needs_attention": [],
         "scorecard": None, "kpis": [], "shift": None, "operations": [], "tomorrow": None, "yesterday": None,
         "insights": [], "actions": [], "missing": [], "withheld": [], "restaurant_id": 7,
         "url": "https://dashboard.cavnar.ai/?rid=7#dsr/2026-09-19"}
    d.update(over)
    return d


def test_the_email_labels_an_estimate_as_one():
    import emails
    kpi = {"key": "food_pct", "label": "Food cost", "value_text": "31.2%", "estimate": True}
    card = {"verdict": {"label": "Good night", "tone": "good"}, "overall": 81, "basis": "",
            "components": [{"key": "food", "label": "Food cost", "measured": True, "value": "1.2 pts over",
                            "detail": "31.2% estimated · your target 30%", "tone": "warn", "estimate": True}]}
    _s, html, _p = emails.dsr_email(_digest(kpis=[kpi], scorecard=card))
    assert "Food cost (est.)" in html
    assert "31.2% estimated" in html


def test_the_email_carries_the_ai_insights_before_the_priorities():
    import emails
    ins = [{"kind": "biggest_win", "label": "Biggest win", "text": "Patio covers doubled."},
           {"kind": "at_stake", "label": "Money at stake", "text": "$1,200 a month — at stake, not saved"}]
    acts = [{"text": "Move a server to the patio.", "key": None, "why": None, "confidence": None}]
    _s, html, _p = emails.dsr_email(_digest(insights=ins, actions=acts))
    assert "AI insights" in html and "Patio covers doubled." in html and "at stake, not saved" in html
    assert html.index("AI insights") < html.index("Tomorrow&rsquo;s priorities")


def test_the_digest_passes_the_views_insights_and_the_location():
    from dsr import deliver
    p = {"business_date": "2026-09-19", "view": "manager", "facts": {"blocks": {}},
         "insights": [{"kind": "biggest_win", "label": "Biggest win", "text": "Patio covers doubled."}, {"x": 1}]}
    d = deliver.digest(p, SimpleNamespace(id=7, name="Simple EJ's", location_name=None))
    assert d["insights"] == [p["insights"][0]]
    assert d["restaurant_id"] == 7
    assert d["url"].endswith("/?rid=7#dsr/2026-09-19")


def test_the_emails_ask_link_names_the_location(monkeypatch):
    import emails
    import rec_delivery
    monkeypatch.setattr(rec_delivery, "presentable", lambda k: True)
    acts = [{"text": "Move a server to the patio.", "key": "dsr_action:7:labor:x", "why": None, "confidence": None}]
    _s, html, _p = emails.dsr_email(_digest(actions=acts))
    assert "src=dsr_email&amp;rid=7" in html


def test_the_email_starting_labor_target_is_not_red():
    from dsr import deliver
    labor = {"status": "ready", "metrics": {"pct": 31.0, "target_pct": 30.0, "vs_target_pts": 1.0},
             "detail": {"target_source": "default"}}
    p = {"business_date": "2026-09-19", "view": "owner", "facts": {"blocks": {"labor": labor}}}
    d = deliver.digest(p, SimpleNamespace(id=7, name="X", location_name=None))
    assert [s["tone"] for s in d["stats"] if s["label"].startswith("Labor")] == ["warn"]
    labor["detail"]["target_source"] = "set"
    d = deliver.digest(p, SimpleNamespace(id=7, name="X", location_name=None))
    assert [s["tone"] for s in d["stats"] if s["label"].startswith("Labor")] == ["bad"]


def test_the_email_tomorrow_carries_its_date():
    import emails
    t = {"date": "2026-09-20", "weekday": "Sunday", "items": [{"text": "School football game"}]}
    _s, html, _p = emails.dsr_email(_digest(tomorrow=t))
    assert "Tomorrow &middot; Sunday 9/20/26" in html


def test_the_managers_email_never_repeats_an_operations_kpi():
    import emails
    k = lambda key, label: {"key": key, "label": label, "value_text": "300"}
    _s, html, _p = emails.dsr_email(_digest("manager", operations=[k("guests", "Guests")],
                                            kpis=[k("guests", "Guests"), k("labor_pct", "Labor")]))
    assert html.count(">Guests<") == 1
