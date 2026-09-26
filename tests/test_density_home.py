"""Density / hierarchy fix round (9/25/26), web Home and the group Home.

#1  the 3-second status: dsr.access.summary carries the scorecard's verdict,
    tone and score, vs budget and the first risk (the shared contract web
    and iPhone read), redacted exactly as the report is.
#19 the group Home: each location's last night, a total strip that never
    mixes two nights, a sentence naming who needs a look.
#20 the brief's DSR "Last night" line is not said again on Home.
#4 #5 #6 #21 #22 #23 #24 #41 #48 the web Home renderer, asserted against
    the source and run through node where the rule is a function.
"""
import json
import re
import shutil
import subprocess
import sys
from datetime import date

import pytest

import auth
import dsr
import home_brief
import models
import pos
from dsr import access, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

SAT = date(2026, 9, 19)
OWNER = {"role": "owner"}
MANAGER = {"role": "manager"}


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    home_brief.invalidate()
    return db_path


def _rest(db, name="Simple EJ's", **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email="e@x.com", timezone="America/Chicago", **kw), db_path=db)
    update_restaurant(rid, {"labor_target_pct": 28.0, "labor_target_source": "set"}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _night(db, rid, day=SAT, net=9000.0, labor_pct=31.0, budget_net=8500.0):
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    m = {"net": net, "guests": 300.0, "avg_ticket": 42.0, "discounts": 120.0}
    if budget_net is not None:
        m.update({"budget_net": budget_net, "vs_budget_net": net - budget_net,
                  "vs_budget_net_pct": (net - budget_net) / budget_net * 100})
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics=m), db_path=db)
    store.save_block(r["id"], "labor", dsr.block(dsr.READY, source="rpower", metrics={
        "pct": labor_pct, "cost": net * labor_pct / 100, "hours": 180.0, "overtime_hours": 3.0, "no_shows": 0}), db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    return store.get_report(rid, day, db_path=db)


# ── #1: the shared contract ────────────────────────────────────────────────

def test_the_owner_summary_carries_the_reports_own_verdict_score_and_budget(db):
    r = _rest(db)
    rep = _night(db, r.id)
    s = access.summary(rep, OWNER, r)
    card = access.render(rep, OWNER, r)["scorecard"]
    # The same scorecard the report draws - never a second reading.
    assert s["verdict"] == card["verdict"]["label"] and s["tone"] == card["verdict"]["tone"]
    assert s["overall"] == card["overall"] and isinstance(s["overall"], int)
    assert s["tone"] in ("good", "warn", "bad")
    assert s["net"] == 9000.0 and s["vs_budget"] == 500.0
    # The first risk is not the budget line the status already carries.
    risks = [x["text"] for x in card["risks"] if x.get("key") != "sales_budget"]
    assert s["first_risk"] == risks[0]


def test_a_manager_never_reads_the_budget_or_a_score_their_report_does_not_have(db):
    r = _rest(db)
    rep = _night(db, r.id)
    store.save_narrative(rep["id"], {
        "executive_summary": {"text": "Sales beat budget by $500.", "cites": ["sales.vs_budget_net"]},
        "operations_summary": {"text": "300 guests on 17 people.", "cites": ["sales.guests"]},
        "went_well": [],
        "needs_attention": [{"text": "Budget missed on the patio.", "cites": ["sales.vs_budget_net"]},
                            {"text": "3 overtime hours on the line.", "cites": ["labor.overtime_hours"]}],
        "actions_tomorrow": []})
    rep = store.get_report(r.id, SAT)
    s = access.summary(rep, MANAGER, r)
    assert s["vs_budget"] is None and s["verdict"] is None and s["overall"] is None and s["tone"] is None
    # The budget line is filtered by its cite; the next one is theirs.
    assert s["first_risk"] == "3 overtime hours on the line."
    assert "budget" not in json.dumps(list(s.values())).lower()


def test_without_the_restaurant_the_score_keys_are_none_not_a_second_reading(db):
    r = _rest(db)
    s = access.summary(_night(db, r.id), OWNER)
    assert s["verdict"] is None and s["overall"] is None and s["first_risk"] is None
    assert s["vs_budget"] == 500.0                     # a stored figure, the owner's


def test_the_list_route_passes_the_restaurant():
    src = open("strategy_routes.py").read()
    assert '"reports": [access.summary(row, u, r) for row in rows]' in src


# ── #19 / #48: the group Home ──────────────────────────────────────────────

def test_the_group_total_sums_one_night_and_never_a_partial_budget():
    locs = [{"last_night": {"business_date": "2026-09-19", "net": 9000.0, "vs_budget": 500.0}},
            {"last_night": {"business_date": "2026-09-19", "net": 7000.0, "vs_budget": None}},
            {"last_night": {"business_date": "2026-09-18", "net": 5000.0, "vs_budget": 100.0}},
            {"last_night": None}]
    t = home_brief.group_last_night(locs)
    assert t["business_date"] == "2026-09-19" and t["label"] == "9/19/26"
    assert t["net"] == 16000.0 and t["locations"] == 2 and t["of"] == 4
    assert t["vs_budget"] is None                      # one of the two has no budget
    locs[1]["last_night"]["vs_budget"] = -200.0
    assert home_brief.group_last_night(locs)["vs_budget"] == 300.0
    assert home_brief.group_last_night([{"last_night": None}]) is None


def test_the_group_sentence_names_the_worst_location_and_its_first_two_issues():
    locs = [{"name": "Wicker Park", "health": "important", "issues": [{"text": "Labor 31.2% — 3.2 pts over target"}]},
            {"name": "Evanston", "health": "critical", "issues": [
                {"text": "3 urgent reviews unanswered"}, {"text": "POS sync failing"}, {"text": "x"}]},
            {"name": "Logan Square", "health": "healthy", "issues": []}]
    assert home_brief.group_summary_line(locs) == (
        "Evanston needs a look: 3 urgent reviews unanswered, POS sync failing · 1 more location too.")
    assert home_brief.group_summary_line([locs[2]]) is None


def test_each_group_row_carries_its_last_night_from_the_report(db, monkeypatch):
    a = _rest(db, "Syrup", location_group="Syrup", location_name="Wicker Park", module_reviews=1)
    b = _rest(db, "Syrup", location_group="Syrup", location_name="Logan Square", module_reviews=1)
    _night(db, a.id, net=9000.0)
    monkeypatch.setattr(home_brief, "get_conn", lambda *x, **k: models.get_conn(db), raising=False)
    user = {"id": 7, "restaurant_id": a.id, "base_restaurant_id": a.id, "role": "owner", "is_admin": 0}
    g, st = home_brief.build_group_brief(user, fresh=True)
    assert st == 200
    rows = {l["name"]: l for l in g["locations"]}
    wp = rows["Wicker Park"]["last_night"]
    assert wp["net"] == 9000.0 and wp["vs_budget"] == 500.0 and wp["verdict"]
    assert rows["Logan Square"]["last_night"] is None
    assert g["portfolio"]["last_night"]["net"] == 9000.0 and g["portfolio"]["last_night"]["of"] == 2
    assert "summary_line" in g


# ── #20: the DSR's "Last night" line is Home's status line's ───────────────

def test_the_dsr_yesterday_line_is_not_presented_on_home():
    import morning_brief
    assert morning_brief.shown_on_home({"key": "yesterday", "source": "dsr"}) is False
    assert morning_brief.shown_on_home({"key": "yesterday"}) is True          # the POS line stays
    assert morning_brief.shown_on_home({"key": "fix_first"}) is False
    assert morning_brief.shown_on_home({"key": "stock"}) is True


# ── the web renderer ───────────────────────────────────────────────────────

SRC = open("templates/dashboard.html", encoding="utf-8").read()
HOME = SRC[SRC.index("  // ── Home renderer (ES5)"):SRC.index("<!-- /panel-home -->")]


def _fn(name, src=SRC):
    m = re.search(r"\n(\s*)function " + re.escape(name) + r"\(.*?\n\1\}", src, re.S)
    assert m, name
    return m.group(0)


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


HELPERS = ("function esc(v){return String(v==null?'':v).replace(/[&<>\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c];});}"
           "function num(v){return esc(v);}function num2(n){return (Math.round(n||0)).toLocaleString('en-US');}"
           "function mdy(x){return 'M('+x+')';}var _hbScope='location';\n")


def test_the_status_line_says_verdict_score_net_and_budget_in_its_tone():
    js = HELPERS + _fn("hbDayGap") + _fn("hbStatusHtml") + """
console.log(JSON.stringify([
 hbStatusHtml({ok:true,tonight:'2026-09-20',reports:[{business_date:'2026-09-19',status:'final',verdict:'Good day',tone:'good',overall:78,net:8420,vs_budget:420}]}),
 hbStatusHtml({ok:true,tonight:'2026-09-20',reports:[{business_date:'2026-09-19',status:'final',net:8420,vs_budget:null,verdict:null,tone:null}]}),
 hbStatusHtml({ok:true,tonight:'2026-09-20',reports:[{business_date:'2026-09-19',status:'final',net:null,missing:['Sales weren\\u2019t in']}]}),
 hbStatusHtml({ok:true,reports:[]})]));"""
    owner, manager, missing, none = _node(js)
    text = re.sub(r"<[^>]+>", "", owner["html"])
    assert owner["tone"] == "good"
    assert "Last night" in text and "Good day 78/100" in text and "$8,420 net" in text and "+$420 vs budget" in text
    t2 = re.sub(r"<[^>]+>", "", manager["html"])
    assert "$8,420 net" in t2 and "budget" not in t2 and "/100" not in t2 and manager["tone"] == ""
    assert "Sales weren" in missing["html"] and "$0" not in missing["html"]      # a missing net is never 0
    assert none is None


def test_needs_attention_opens_with_one_deterministic_sentence():
    js = _fn("hbAttnLine") + """
console.log(JSON.stringify([
 hbAttnLine([{severity:'critical',title:'Reply to the Hendersons\\u2019 1\\u2605 review'},{severity:'important',title:'Salmon runs out by 6pm.'},
             {severity:'critical',title:'POS sync failed'},{severity:'watch',title:'x'}],[{status:'open',severity:'low',title:'Walk-in door'}]),
 hbAttnLine([{severity:'important',title:'Trim Tuesday lunch'}],[]),
 hbAttnLine([{severity:'watch',title:'Rating steady'}],[]),
 hbAttnLine([],[])]));"""
    a, b, c, d = _node(js)
    # Counts only, never the rows' own words (9/25/26: one item was said
    # twice, once in this line and once in its row); nothing under two items.
    assert a == "5 flagged: 2 need you now, 2 worth handling today, 1 to watch."
    assert b == "" and c == "" and d == ""


def test_the_results_row_carries_the_measured_figure_never_an_opportunity():
    js = HELPERS + "var GOAL_ON={met:1,at_target:1,moving_right_way:1};" + _fn("hbResultsLine") + """
console.log(JSON.stringify([
 hbResultsLine({total:1517,net_monthly:917,worsened:{count:1,monthly:600},opportunity:9999},
   [{verdict:'improved'},{verdict:'improved'},{verdict:'improved',counts:false},{verdict:'worsened'}],
   [{state:'met'},{state:'moving_wrong_way'},{state:'moving_right_way'}]),
 hbResultsLine({total:0},null,null)]));"""
    full, empty = _node(js)
    assert full == "2 improved · 1 worse · $917/mo net measured · 2 of 3 goals on track"
    assert "9,999" not in full
    assert empty.startswith("Nothing measured yet")


def test_the_confidence_pill_is_the_shared_component_and_opens_why():
    a = SRC.index('<script id="cav-conf">')
    conf = SRC[a:SRC.index("</script>", a)]
    body = conf[conf.index("  function line(c,o){"):]
    assert "if(o.pill){" in body and 'data-explain-title="How sure is this?"' in body
    attn = _fn("renderAttention")
    assert "surface:'home',module:'home',pill:1}" in attn
    assert "cls:'hb-conf',pill:1}" in _fn("hbRecCard")


def test_home_has_one_primary_button_the_focus_cards():
    # In-place confirmations (publish, hand-over) and the Data Health
    # drawer are transient editors, not the page.
    persistent = [m.start() for m in re.finditer("cbtn-primary", HOME)]
    allowed = ("data-go=\"1\"", "Hand it over", "data-dh-sync")
    left = [i for i in persistent if not any(x in HOME[i:i + 200] for x in allowed)]
    focus = _fn("renderFocus")
    assert len(left) == focus.count("cbtn-primary") == 4          # four exclusive branches of one button
    for name in ("renderAttention", "hbRecCard", "renderQuick", "renderCloseout", "renderGroup"):
        assert "cbtn-primary" not in _fn(name), name


def test_the_focus_card_holds_one_thing_and_freshness_is_said_once():
    focus = _fn("renderFocus")
    assert "hbLive(" not in focus and "hbLive(" not in _fn("renderRecs")
    # The header carries no data chip (owner's call, 9/26/26): the strip
    # below speaks only when a source is behind.
    assert "hbDataChip(d)" not in _fn("renderTop")
    assert "if(hbFreshBehind(d))h+=renderFreshness(d);" in _fn("render")


def test_decisions_leave_collapsed_results_for_needs_attention():
    follow = _fn("renderFollow")
    assert "_hbAttnExtra=hbLossRows(g.loss).concat(hbLinkRows(g.cross));" in follow
    assert "hbAttnRedraw(d);" in follow
    loss = _fn("renderLoss")
    assert "recControlsHtml" not in loss
    assert "function renderConnections(" not in SRC     # removed 9/25/26: the links are rows
    assert "recControlsHtml(f.rec_key,'home','ops')" in _fn("hbLossRows")
    assert "recControlsHtml(x.rec_key,'home','home')" in _fn("hbLinkRows")


def test_the_day_holds_only_todays_work():
    follow = _fn("renderFollow")
    assert "renderCloseout(hour<18)" in follow and "evening=hour>=20" in follow
    assert "if(dayEl){dayEl.innerHTML=day+_hbDayOnly(h);}" in follow
    assert "h+=(monday?'':receipts);" in follow
    co = _fn("renderCloseout")
    assert "if(collapsed)return '<details" in co and "Write it →" in co


def test_group_home_shows_every_item_above_the_table_and_the_night_per_location():
    g = _fn("renderGroup")
    assert "Math.min(at.length,6)" not in g
    assert "data-attn-more" in g and "hb-attn-more" in g and 'id="hb-attn-card"' in g
    # The table opts into sorting (web desk #1), so its tag carries data-sortable.
    assert g.index('id="hb-attn-card"') < g.index('<table class="hb-tbl"')
    assert "hbNightCell(r.last_night)" in g and "hbGroupTotal(p)" in g and "g.summary_line" in g
    assert '<th class="r">Labor</th>' in g                       # always visible now
    assert "<h3>" not in g                                     # #41: .hb-h3 only
    assert "hbSwitcherFill(g)" in g
    assert 'data-loc-id="{{ loc.id|int }}"' in SRC and "window.hbSwitcherLoad()" in SRC


def test_home_section_heads_are_two_levels():
    for name in ("renderFocus", "hbMilestone", "renderWelcome", "renderGroup", "renderHero"):
        assert 'class="hb-kicker"' not in _fn(name), name
    # Results is an orange kicker with nothing beside it (owner, 9/26/26),
    # like Reviews' Trends.
    assert '<summary><span class="hb-kicker">Results</span></summary>' in _fn("render")
