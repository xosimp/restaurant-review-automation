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


def test_the_email_puts_the_priorities_before_at_most_two_insights():
    # 9/25/26 (ID1-23): priorities right after wins and risks; insights after
    # the numbers, two at most. This pinned insights before the priorities.
    import emails
    ins = [{"kind": "biggest_staffing_concern", "label": "Staffing", "text": "Patio needs a runner."},
           {"kind": "largest_guest_experience", "label": "Guests", "text": "Waits ran long at 7pm."},
           {"kind": "largest_opportunity", "label": "Biggest opportunity", "text": "Third insight."}]
    acts = [{"text": "Move a server to the patio.", "key": None, "why": None, "confidence": None}]
    _s, html, _p = emails.dsr_email(_digest(insights=ins, actions=acts))
    assert "AI insights" in html and "Patio needs a runner." in html and "Third insight." not in html
    assert html.index("Tomorrow&rsquo;s priorities") < html.index("AI insights")


def test_insights_never_restate_a_win_a_risk_or_a_priority():
    # ID1-18: biggest win / risk / top priority are said above; the Food
    # block's money at stake is its own tile, not an insight (ID1-16).
    from dsr import access
    n = {"biggest_win": {"text": "Patio covers doubled."}, "biggest_risk": {"text": "Buns run out."},
         "highest_priority_issue": {"text": "Order buns."},
         "biggest_staffing_concern": {"text": "Move a server to the patio."},
         "largest_guest_experience": {"text": "Waits ran long at 7pm."},
         "largest_opportunity": {"text": "Beer mix is light."},
         "actions_tomorrow": [{"text": "Move a server to the patio."}]}
    facts = {"blocks": {"food": {"status": "ready", "metrics": {"drivers_at_stake_monthly": 1200.0}}}}
    out = access.insights(facts, n, access.OWNER, said=access._said(None, n))
    assert [i["kind"] for i in out] == ["largest_guest_experience", "largest_opportunity"]
    assert len(out) <= access.INSIGHTS_MAX
    assert not any(i["kind"] in access.RESTATED_INSIGHTS or i["kind"] == "at_stake" for i in out)


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


# ── the density round (9/25/26): SCORE FIRST, say each thing once ──────────

def _card(**over):
    c = {"verdict": {"label": "Good day", "tone": "good"}, "overall": 78, "basis": "A weighted score",
         "components": [{"key": "sales", "label": "Sales", "measured": True, "value": "+$420 vs budget",
                         "detail": "$8,420 net · +5.2%", "tone": "good", "score": 88},
                        {"key": "labor", "label": "Labor", "measured": True, "value": "on target",
                         "detail": "27.5% of sales · target 28%", "tone": "good", "score": 85},
                        {"key": "food", "label": "Food cost", "measured": False, "value": None,
                         "why": "Not estimated for this night"},
                        {"key": "guests", "label": "Guest experience", "measured": False, "value": None,
                         "why": "No new reviews"}],
         "wins": [{"text": "Win %d." % i} for i in range(1, 6)],
         "risks": [{"text": "Risk %d." % i} for i in range(1, 5)]}
    c.update(over)
    return c


def _k(key, label):
    return {"key": key, "label": label, "value_text": "1"}


def _owner_night():
    p = _payload()
    p["facts"]["blocks"]["sales"] = {"status": "ready", "source": "rpower", "metrics": {"net": 8420.0}, "detail": {}}
    p.update(scorecard=_card(),
             kpis=[_k("net", "Net sales"), _k("labor_pct", "Labor %"), _k("prime_pct", "Prime cost"),
                   _k("avg_ticket", "Average ticket"), _k("guests", "Guests"), _k("splh", "Sales per labor hour"),
                   _k("bev_mix", "Beverage mix")],
             kpis_headline=["prime_pct", "avg_ticket", "guests", "splh"],
             insights=[{"kind": "largest_staffing", "label": "Staffing", "text": "Patio needs a runner."}],
             tomorrow={"weekday": "Sunday", "date": "2026-09-20", "items": []})
    p["narrative"] = {"executive_summary": {"text": "A steady Saturday."},
                      "actions_tomorrow": [{"text": "Action %d." % i} for i in range(1, 5)]
                      + [{"text": "Add a server to the Sunday schedule.", "kind": "staffing"}],
                      "verification": {"checked": 9, "kept": 8, "dropped": 1, "measured": 7}}
    p["checklist"] = {"stages": [{"label": "Ready", "at_local": "2026-09-19T23:40"}]}
    return p


def _plain(html):
    return html.replace('<span class="hb-num">', "").replace("</span>", "")


def test_the_owner_report_reads_score_first_then_the_actions():
    html = _render(_owner_night())
    order = [html.index(k) for k in ('aria-label="Today’s score"', 'aria-label="Executive summary"',
                                     "Today’s wins", "Tomorrow’s priorities", ">Sunday", "Top KPIs",
                                     "AI insights", "Block by block", "How the night was built")]
    assert order == sorted(order)
    # The score is the hero, with the night's net the biggest figure on it;
    # the summary is no longer a hero.
    score = html[html.index('dr-score"'):html.index('aria-label="Executive summary"')]
    assert "hero" in html[html.index('dr-score"') - 40:html.index('dr-score"')]
    assert '<div class="net">' in score and "8,420" in score and "+$420 vs budget" in score
    assert 'class="hb-card dr-read"' in html
    # Sales is the hero, not a component tile beside it.
    assert '<div class="k">Sales</div>' not in score


def test_the_owner_report_says_each_thing_once():
    html = _render(_owner_night())
    top = html[html.index("Top KPIs"):html.index('id="dr-allk"')]
    for label in ("Prime cost", "Average ticket", "Guests", "Sales per labor hour"):
        assert '<div class="k">%s' % label in top
    assert "Net sales" not in top and "Labor %" not in top      # Today's score says them
    allk = html[html.index('id="dr-allk"'):]
    allk = allk[:allk.index("</details>")]
    assert "Net sales" in allk and "Labor %" in allk and "Beverage mix" in allk
    # Three wins and three risks up front, then "N more"; three priorities.
    plain = _plain(html)
    assert "2 more</summary>" in plain and "1 more</summary>" in plain
    assert "2 more priorities</summary>" in plain
    # Tomorrow points to the staffing priority instead of repeating it.
    assert html.count("Add a server to the Sunday schedule.") == 1
    assert "see priority #5 above" in plain and "AI recommendation" not in html


def test_the_blocks_are_closed_and_the_footer_is_in_how_it_was_built():
    html = _render(_owner_night())
    assert re.search(r'id="dr-sec-\w+" open>', html) is None
    built = html[html.index('id="dr-built"'):]
    assert "dr-foot" in built and "8 of 9 lines kept" in _plain(built)
    assert "dr-foot" not in html[:html.index('id="dr-built"')]
    # No score to read: Sales opens on its own.
    p = _owner_night()
    p["scorecard"] = None
    assert 'id="dr-sec-sales" open>' in _render(p)


def test_the_manager_report_leads_with_labor_against_target():
    p = _payload("manager", kpis=[_k("labor_pct", "Labor"), _k("rating", "Guest rating")],
                 kpis_headline=["labor_pct", "rating"],
                 operations=[_k("avg_ticket", "Average ticket"), _k("guests", "Guests")],
                 shift={"rows": [{"key": "no_shows", "label": "No-shows", "value_text": "1", "tone": "warn"}],
                        "verdict": {"text": "Labor 31.0%, 1.0 pts over starting target · 1 no-show", "tone": "warn"}})
    html = _render(p)
    assert html.index("Today’s shift") < html.index("Top KPIs") < html.index(">Operations<")
    assert 'class="dr-shift-v warn"' in html and "1 no-show" in html


def _week(**over):
    days = [{"date": "2026-09-14", "weekday": "Mon", "net": 5000.0, "budget_net": 5200.0, "status": "final",
             "last_year_net": 4800.0},
            {"date": "2026-09-15", "weekday": "Tue", "net": 8420.0, "budget_net": 8000.0, "status": "final"},
            {"date": "2026-09-16", "weekday": "Wed", "net": None, "budget_net": 6000.0}]
    g = {"start": "2026-09-14", "end": "2026-09-20", "days": days, "categories": [], "withheld": [],
         "totals": {"net": 13420.0, "days_measured": 2, "vs_budget_net": 220.0, "vs_budget_net_pct": 1.7,
                    "labor_pct": 27.4},
         "story": "Tuesday carried the week ($8,420 net, 63% of it); Monday missed budget by $200."}
    g.update(over)
    return g


def test_the_week_leads_with_its_summary_tiles_bars_and_sentence():
    out = _node("console.log(window.__t.weekHtml(%s,'owner'));" % json.dumps(_week()))
    s = out[out.index("hb-card dr-wsum"):out.index('class="dr-gridbox')]
    for k in ("Week to date · net", "vs budget", "vs last year", "Labor %"):
        assert k in s
    assert "13,420" in s and "+$220" in s
    bars = re.findall(r'<div class="c( \w+)?" style', s)
    assert bars == [" miss", " met", ""]            # Wednesday was not measured
    assert s.count('<em class="bud"') == 3 and "Tuesday carried the week" in s
    # Last year exists: the import card is a link that opens it.
    assert 'data-dr="imp-open"' in out and 'id="dr-imp" hidden' in out
    g = _week()
    g["days"][0].pop("last_year_net")
    out = _node("console.log(window.__t.weekHtml(%s,'owner'));" % json.dumps(g))
    assert 'data-dr="imp-open"' not in out and 'id="dr-imp" aria-label' in out


def test_the_managers_week_summary_has_no_budget():
    g = _week(withheld=["budget"], story="Tuesday carried the week ($8,420 net, 63% of it).")
    for d in g["days"]:
        d.pop("budget_net")
    g["totals"].pop("vs_budget_net")
    g["totals"].pop("vs_budget_net_pct")
    out = _node("console.log(window.__t.weekHtml(%s,'manager'));" % json.dumps(g))
    s = out[out.index("hb-card dr-wsum"):out.index('class="dr-gridbox')]
    assert "budget" not in s.lower() and 'class="bud"' not in s
    assert "Week to date · net" in s and "Labor %" in s


def test_the_email_reads_score_first_with_three_wins_and_four_kpis():
    import emails
    kp = [_k(k, k.upper()) for k in ("net", "labor_pct", "prime_pct", "avg_ticket", "guests", "splh", "bev_mix")]
    acts = [{"text": "Action %d." % i, "key": None, "why": None, "confidence": None} for i in range(1, 6)]
    y = {"items": [{"text": "Sales between $8,000 and $9,500", "outcome": "correct"},
                   {"text": "Rain", "outcome": "incorrect"}], "accuracy": {"pct": None, "correct": 3, "graded": 4}}
    _s, html, _p = emails.dsr_email(_digest(scorecard=_card(), kpis=kp, actions=acts, yesterday=y,
                                            kpis_headline=["prime_pct", "avg_ticket", "guests", "splh"],
                                            tomorrow={"weekday": "Sunday", "date": "2026-09-20",
                                                      "items": [{"text": "Game day"}]}))
    order = [html.index(k) for k in ("Today&rsquo;s score", "$9,000", "Executive summary", "Today&rsquo;s wins",
                                     "Today&rsquo;s risks", "Tomorrow&rsquo;s priorities", "Tomorrow &middot;",
                                     "Top KPIs", "Yesterday&rsquo;s predictions:")]
    assert order == sorted(order)
    assert "Win 3." in html and "Win 4." not in html and "Risk 4." not in html
    assert ">PRIME_PCT<" in html and ">NET<" not in html and ">BEV_MIX<" not in html
    assert "Action 3." in html and "Action 4." not in html and "2 more in the full report" in html
    assert "1 of 2 correct" in html and "3 of 4 right so far" in html
    assert "Sales between $8,000" not in html        # each prediction is on the full report
