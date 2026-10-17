"""Food Cost + Bar audit fix round, workstream F (10/16/26): the Food Cost web
panel and the iPhone Food Cost screens — findings UI-01 to UI-18 of
/Users/simp/fc_bar_audit_1016/06-ui.md (the Bar's UI-13/UI-19/UI-20 are
workstream C1's).

Server halves run through the routes; the page's geometry runs under node;
the rest are source pins on dashboard.html and the Swift files, each naming
the finding it holds.

UI-10's deletions, traced per CLAUDE.md before removal (rg over the repo,
docs/history excluded):
  - fc2ImportRecipes: one definition, no caller anywhere (no onclick, no
    window[...] lookup, no test, no doc, no Swift); it read #fc2-import-csv,
    an element removed 9/26/26. Deleted.
  - the paste path of fc2DraftRecipes (#fc2-menu-text): the textarea went
    9/26/26 (test_recipes_from_menu holds it absent); the one caller passes
    missingOnly. The function stays, drafting the POS dishes only; the
    POST /food-cost/recipes/draft and /recipes/import routes stay (the
    phone uses them).
  - loadMenuMargins / #mm-body / #fc2-tools: callers were the panel's own
    whenSeen and the Refresh button inside it; no nav head, no scheduler,
    no Swift. The phone's "inventory/margins" web rows (MenuMarginsSheet,
    the Analytics section) are kept working: data-nav="inventory/margins"
    moved to the dish table. GET /api/food-cost/menu-profitability stays
    (the phone's Menu margins sheet reads it).
  - the #fc2-today-drv writer in fc2LoadCfo: the element was removed
    9/26/26; nothing else reads or writes the id.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import date, timedelta

import pytest

import backoffice_ap
import client_api
import inventory
import mobile_api
from tests.test_bar_routes import MOBILE, _app, _as, _conn, _rid, db  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
IOS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _swift(rel):
    return open(os.path.join(IOS, rel), encoding="utf-8").read()


def _between(a, b, src=SRC):
    i = src.index(a)
    return src[i:src.index(b, i + len(a))]


PANEL = _between('<div class="panel" id="panel-inventory"', "{% elif fc_enter_only %}")


# ── UI-01: the phone never shows the sample pantry to a Back Office kitchen ──

_SAMPLE = {
    "waste_items": [{"item": "Romaine", "waste_cost": 40.0}], "overstock": [{"item": "Flour", "overstock_cost": 90.0}],
    "critical_low": [{"item": "Chicken", "days_remaining": 1}], "reorder_soon": [{"item": "Salt"}],
    "order_reduction": [{"item": "Rice"}], "total_waste_cost_week": 312.0, "overstock_total": 90.0,
    "waste_items_total": 40.0, "total_stock_value": 4200.0, "total_items": 3, "recoverable_monthly": 120.0,
    "annual_recoverable": 1440.0, "monthly_waste_projection": 1350.0, "annual_waste_projection": 16224.0,
    "waste_rate_pct": 6.1, "benchmark_label": "Over target", "week_start": "10/9/26", "week_end": "10/15/26",
}


def _analytics(monkeypatch, rid, on_file):
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([{"item": "Romaine"}], False, dict(_SAMPLE)))
    monkeypatch.setattr(client_api, "food_insight_text", lambda *a, **k: "Your Back Office week.")
    monkeypatch.setattr(client_api, "food_read_backoffice", lambda *a, **k: None)
    monkeypatch.setattr(backoffice_ap, "feed_state", lambda r, db_path=None: {
        "files": 2 if on_file else 0, "last_import": None, "invoices_through": "2026-10-01" if on_file else None,
        "recipes_loaded": None, "on_file": on_file})
    import pos_food_waste
    monkeypatch.setattr(pos_food_waste, "remakes", lambda r, days=7, today=None, db_path=None: {
        "available": True, "window": {"from": "2026-10-09", "to": "2026-10-15", "days": 7},
        "items": [{"name": "Smash Burger", "qty": 2.0, "plate_cost": 4.25, "cost": 8.5, "reason": "Error Made"}],
        "uncosted": [], "total_cost": 8.5})
    _as(monkeypatch, rid, role="client")
    return _app().test_client().get("/mobile/api/food-cost/analytics", headers=MOBILE).get_json()


def test_ui01_a_back_office_kitchen_gets_none_of_the_sample_pantry_on_the_phone(db, monkeypatch):
    body = _analytics(monkeypatch, _rid(db), on_file=True)
    assert body["ok"] is True and body["source"] == "back_office" and body["is_live"] is False
    for k in ("waste_items", "overstock", "critical_low", "reorder_soon", "order_reduction", "price_watch"):
        assert body[k] == [], k
    # A figure nobody measured is no figure - never the sample's, never 0.
    for k in ("total_waste_cost_week", "overstock_total", "total_stock_value", "recoverable_monthly",
              "annual_waste_projection", "waste_rate_pct"):
        assert body[k] is None, k
    assert body["food_source"]["label"] == "Back Office" and body["food_source"]["invoices_through_mdy"] == "10/1/26"
    assert body["made_not_sold"]["total_cost"] == 8.5
    assert (body["made_not_sold"]["from_mdy"], body["made_not_sold"]["to_mdy"]) == ("10/9/26", "10/15/26")
    assert body["count_line"] == "Stock and over-par come with your next Back Office count."
    assert body["insight"] == "Your Back Office week."


def test_ui01_a_kitchen_with_no_back_office_is_sample_and_keeps_its_example_rows(db, monkeypatch):
    body = _analytics(monkeypatch, _rid(db), on_file=False)
    assert body["source"] == "sample" and body["made_not_sold"] is None and body["count_line"] is None
    assert body["critical_low"] and body["total_waste_cost_week"] == 312.0


def test_ui01_the_phone_keys_example_data_on_the_source_and_reads_back_office():
    model = _swift("Models/FoodCostAnalytics.swift")
    assert "if let source { return source == \"sample\" }" in model and "return isLive == false" in model
    assert 'case foodSource = "food_source"' in model and 'case madeNotSold = "made_not_sold"' in model
    assert "struct FoodCostPurchases: Decodable" in model and "struct FoodCostPlates: Decodable" in model
    assert 'case daysBehind = "days_behind"' in model and 'case toConfirm = "to_confirm"' in model
    card = _swift("Features/FoodCost/FoodCostBackOfficeCard.swift")
    assert '"/mobile/api/food-cost/purchases"' in card and '"/mobile/api/food-cost/plates"' in card
    assert "4 weeks to" in card and "purchase-based, not food cost" in card
    assert 'path: "inventory/plates"' in card and 'data-nav="inventory/plates"' in SRC
    # A withheld figure is a dash, never a zero.
    assert 'Text("\\u{2014}").cavnarText(.figureL, color: .cavnarInk3)' in card
    sec = _swift("Features/FoodCost/FoodCostAnalyticsSection.swift")
    assert "FoodCostBackOfficeCard(viewModel: viewModel.backOffice, feedLabel: analytics.feedLabel)" in sec
    assert "if analytics.foodSource?.onFile == true {" in sec
    assert "!a.criticalLow.isEmpty, !a.showsExampleData" in sec, "the sample's order is never pinned"
    assert 'Text("Made and not sold \\u{00B7} last 7 days")' in sec
    vm = _swift("Features/FoodCost/FoodCostAnalyticsViewModel.swift")
    assert "if analytics?.foodSource?.onFile == true {\n            await backOffice.load()" in vm
    # The new file sits in the app's sources (XcodeGen reads the folder).
    assert "- path: CavnarAI" in open(os.path.join(ROOT, "ios", "CavnarAI", "project.yml"), encoding="utf-8").read()
    # No POS named in the example caveat (UI-08 / UI-18).
    assert "Connect Toast" not in _swift("DesignSystem/CavnarCaveat.swift")
    assert "Connect Toast" not in inventory.SAMPLE_DATA_NOTICE


# ── UI-02: the waste trend's axes and drawing share one geometry ──────────────

def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_ui02_bars_and_both_axes_are_one_geometry_at_200_760_and_900px():
    geom = _between("var WT_H=", "function wtDrawChart(){")
    js = geom + """
var out=[];
[200,760,900].forEach(function(width){[8,13,26,52].forEach(function(n){
  var G=wtGeom(width,n),top=1000,bars=[],labels=[];
  for(var i=0;i<n;i++){bars.push([G.x(i),G.x(i)+G.bw,G.cx(i)]);if(G.labelled(i))labels.push(i);}
  out.push({width:width,n:n,W:G.W,L:G.L,bw:G.bw,gap:G.gap,bars:bars,labels:labels,
            yTop:G.y(top,top),yZero:G.y(0,top),base:G.base,padT:G.padT,step:G.bw+G.gap});
});});
console.log(JSON.stringify(out));
"""
    for g in json.loads(_node(js)):
        # The viewBox is the box's own width: 1:1, no letterbox, no stretch.
        assert g["W"] == g["width"], g["width"]
        assert g["bw"] > 0 and g["gap"] > 0
        first, last = g["bars"][0], g["bars"][-1]
        # Every bar sits in the plot, right of the dollar gutter, inside W.
        assert first[0] >= g["L"] and last[1] <= g["W"] + 1e-6
        for x0, x1, cx in g["bars"]:
            assert abs(cx - (x0 + x1) / 2) < 1e-9, "a week label is centred on its bar's own centre"
        # The dollar scale: the top label on the top gridline, $0 on the base.
        assert abs(g["yTop"] - g["padT"]) < 1e-9 and abs(g["yZero"] - g["base"]) < 1e-9
        # The newest week is always labelled; labels never crowd (≥ 52px apart
        # unless only the newest is left).
        assert g["labels"][-1] == g["n"] - 1
        for a, b in zip(g["labels"], g["labels"][1:]):
            assert (b - a) * g["step"] >= 40 or b == g["n"] - 1, (g["width"], g["n"], a, b)


_DOM = """
var _els={};
function _el(id){if(!_els[id])_els[id]={id:id,hidden:false,clientWidth:0,innerHTML:'',dataset:{},style:{},
  classList:{add:function(){},remove:function(){},toggle:function(){}},addEventListener:function(){},
  setAttribute:function(){},getAttribute:function(){return null;},querySelector:function(){return null;},
  querySelectorAll:function(){return [];}};return _els[id];}
var window={matchMedia:function(){return {matches:true};},addEventListener:function(){}};
var document={getElementById:_el,addEventListener:function(){},querySelector:function(){return null;},
  querySelectorAll:function(){return [];},fonts:{status:'loaded'}};
"""


def test_ui02_the_chart_draws_at_the_boxs_own_width_with_its_axes_inside():
    """The real wtDrawChart, under node, at a phone's 200px and a desktop's
    900px: the SVG is the box's width, the dollar labels and the week labels
    are SVG text, and every week is a keyboard button."""
    block = _between("// ── Waste trend card", "var selCt=window.CAV_BOOT.selCt;")
    weeks = [{"label": f"9/{d}", "start_label": f"9/{d - 6}", "waste": 100 + 20 * i, "flags": {}}
             for i, d in enumerate((7, 14, 21, 28))]
    payload = {"ok": True, "range": "8w", "weeks": weeks, "stats": {}, "target": {"weekly": 150, "pct": 4.5}}
    js = _DOM + block + """
var out={};
[200,900].forEach(function(w){_el('fc2-wt-chart').clientWidth=w;_wt.data=%s;_wt.animated={};wtDrawChart();
  out[w]=_el('fc2-wt-chart').innerHTML;});
console.log(JSON.stringify(out));
""" % json.dumps(payload)
    out = json.loads(_node(js))
    for w, html in out.items():
        assert f'viewBox="0 0 {w} 256" width="{w}" height="256" preserveAspectRatio="none"' in html, w
        assert html.count('class="fc2-wt-ylab"') == 5 and "$0</text>" in html
        assert html.count('class="fc2-wt-hit" data-i=') == 4 and html.count('role="button" tabindex="0"') == 4
        assert 'class="fc2-wt-xlab' in html and ">9/28</text>" in html, "the newest week is labelled"
        assert '<div class="y">' not in html and '<div class="ax">' not in html


def test_ui02_the_axes_are_drawn_inside_the_svg_from_the_bars_own_xy():
    chart = _between("function wtDrawChart(){", "\nfunction wtTipPos")
    assert 'preserveAspectRatio="none"' in chart and "xMidYMid meet" not in chart
    assert "var G=wtGeom(host.clientWidth,n)" in chart and "wtWatchWidth(host);" in chart
    assert "(y(steps[q])+5)" in chart and "G.cx(i).toFixed(1)" in chart
    assert "<div class=\"y\">" not in chart and "<div class=\"ax\">" not in chart
    assert ".fc2-wt-chart .y{" not in SRC and ".fc2-wt-chart .ax{" not in SRC
    assert "new ResizeObserver(" in SRC[SRC.index("function wtWatchWidth(host){"):]


# ── UI-03 / UI-04 / UI-05: one story, dates beside every figure ──────────────

def test_ui03_a_back_office_kitchen_reads_one_story():
    assert "{% set _bo_next = 'Stock and over-par come with your next ' ~ _bo_label ~ ' count.' %}" in PANEL
    assert PANEL.count("{{ _bo_next }}") >= 2, "the tip and the order list say the same next step"
    assert "Made and not sold · last 7 days" in PANEL
    assert "Waste and stock on hand come with your first count" not in PANEL
    assert "first kitchen count" not in SRC
    # Cash over par and the auto-order switch wait for a count.
    bo = _between("{% if _bo_wait %}\n  {# A Back Office kitchen (10/16/26)", "{% else %}")
    assert "Cash sitting over par" not in bo and "fc2-stock-one" in bo
    assert '{% if inv.is_live %}<span class="rul-sw" id="fc2-autoorder-sw"' in PANEL
    assert "if (orders && $id('fc2-autoorder-sw') && window.cavnarWhenVisible)" in SRC


def test_ui04_the_figures_carry_their_dates_not_this_week():
    assert "_remakes(restaurant.id, today=_rt_mns(restaurant.id))" in open(
        os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    assert '<div class="fc2-num-lbl">Waste this week</div>' not in PANEL
    assert "Waste · <span class=\"hb-num\">{{ inv.week_start }} – {{ inv.week_end }}</span>" in PANEL
    assert "<small>this week</small>" not in PANEL and "<small>last 7 days</small>" not in PANEL
    assert "of this week\\u2019s waste" not in SRC and "of the last '+(+d.window_days||7)+' days" in SRC
    assert "tile('This week'," not in SRC and "'Week of '+wtEsc(lw.start_label)" in SRC
    assert "Waste this week" not in _swift("Features/FoodCost/FoodCostAnalyticsSection.swift")


def test_ui05_bought_vs_sold_names_its_window_and_its_lag(db, monkeypatch):
    rid = _rid(db)
    today = date.today()
    end = (today - timedelta(days=20)).isoformat()
    monkeypatch.setattr(mobile_api, "_bar_count_today", lambda r: today)
    monkeypatch.setattr(backoffice_ap, "food_bought_vs_sold", lambda r, today=None, db_path=None: {
        "available": True, "label": "Food bought vs sold",
        "window": {"from": "2026-09-01", "to": end, "from_mdy": "9/1/26", "to_mdy": "x", "pct": 41.0}})
    _as(monkeypatch, rid, role="client")
    w = _app().test_client().get("/api/food-cost/purchases").get_json()["window"]
    assert w["days_behind"] == 19, "the window's end against the restaurant's yesterday"
    fn = _between("function fc2LoadBo(){", "var FC2_PL_SHOW=")
    assert "bought vs sold · 4 weeks to" in fn and "w.days_behind>7" in fn
    assert "last four weeks" not in fn


# ── UI-06: a card hides only when available === false ────────────────────────

def test_ui06_an_error_says_the_servers_sentence_and_never_hides_the_card():
    for fn, kicker in (("function fc2LoadBo(){", "Food bought vs sold"), ("function fc2LoadPlates(){", "Plate costs")):
        body = _between(fn, "\n}\n")
        assert "if(d&&d.ok&&d.available===false){host.hidden=true;return;}" in body, fn
        assert f"if(!d||!d.ok){{fc2CardError(host,'{kicker}',d);return;}}" in body, fn
        assert "host.hidden=true;})" not in body.replace(" ", ""), fn
    cfo = _between("function fc2LoadCfo(){", "\n}\n")
    assert "if(!d||!d.ok){fc2CardError(host,'Where the money is going',d);return;}" in cfo
    assert "fc2TileError(host,'Recipe coverage',d)" in SRC and "fc2TileError(host,'Unexplained',d)" in SRC
    menu = _between("function loadFcMenu(){", "\n  }\n")
    assert "sec.style.display='none'" not in menu and "(d&&d.error)" in menu
    helper = _between("window.fc2CardError=function(host,kicker,d,fallback){", "};")
    assert "(d&&(d.error||d.why_not))" in helper and "host.hidden=false" in helper


# ── UI-07: Send to suppliers sits with What to order this week ───────────────

def test_ui07_the_send_fold_is_at_the_foot_of_the_order_list_and_loads_when_seen():
    orders = _between('<div class="fc2-sec" id="fc2-orders">', 'data-nav="inventory/stock">')
    assert '<details class="hb-results fc2-work fc2-send-d" id="fc2-order-send-d">' in orders
    sup = _between('<details class="hb-results fc2-work" id="fc2-work-suppliers" hidden>', "</details>")
    assert "fc2-order-send-d" not in sup
    assert "whenSeen('fc2-order-send-d',loadOrderDraft);" in SRC


# ── UI-08 / UI-09: example data and a stale count ─────────────────────────────

def test_ui08_sample_sections_are_dimmed_and_tagged_and_name_no_pos():
    assert "{% set _example = not inv.is_live and not _bo %}" in PANEL
    assert "<div class=\"fc2-orders{{ ' fc2-example' if _example }}\">" in PANEL
    assert "fc2-stock-sig{{ ' fc2-example' if _example }}" in PANEL
    assert PANEL.count('<span class="fc2-ex-tag">example</span>') == 3
    assert ".fc2-example{opacity:.55}" in SRC
    assert "why=d.notice||" not in SRC, "the server's sample notice named a POS"


def test_ui09_a_stale_count_is_a_neutral_wait_never_green():
    order = _between("{% if _bo_wait %}\n    {# An order list reads the shelf", "{% elif inv.critical_low")
    assert "{% elif _inv_stale %}" in order and "The order list waits on a fresh count" in order
    assert "fc2-ok fc2-wait" in order
    assert "('neutral' if _inv_stale else ('good' if inv.critical_low|length == 0 else 'bad'))" in PANEL
    # The green all-clear is for a live, fresh count only.
    assert "{% elif inv.is_live %}\n    <div class=\"fc2-ok\"><i></i>Your ordering looks well-calibrated" in PANEL


# ── UI-10: one dish table ─────────────────────────────────────────────────────

def test_ui10_menu_margins_folded_into_the_dish_table_and_the_dead_code_is_gone():
    for gone in ("function fc2ImportRecipes", "fc2-import-csv", "fc2-menu-text", "function loadMenuMargins",
                 'id="mm-body"', 'id="fc2-tools"', "fc2-today-drv", "% fc</td>"):
        assert gone not in SRC, gone
    menu = _between("function loadFcMenu(){", "\n  }\n")
    assert "<th class=\"r\">Plate cost</th><th class=\"r\">Price</th><th class=\"r\">Plate cost %</th>" in menu
    assert "onchange=\"saveMenuPrice(this)\"" in menu and "aria-label=\"Price of '" in menu
    assert "[x.cost_tone]" in menu and "No dish margins yet" in menu and "Draft them" in menu
    assert 'id="fc2-score-body" data-nav="inventory/margins"' in PANEL
    assert "function fc2DraftRecipes(btn){" in SRC and "onclick=\"fc2DraftRecipes(this)\"" in SRC


def test_ui10_the_dish_table_carries_menu_margins_colour(monkeypatch):
    import cogs
    import menu_intelligence
    import strategy_routes
    monkeypatch.setattr(menu_intelligence, "dish_scorecard", lambda rid: {
        "available": True, "dishes": [{"name": "Burger", "food_cost_pct": 28.0}, {"name": "Steak", "food_cost_pct": 52.0}]})
    monkeypatch.setattr(cogs, "dish_reference", lambda r: {"pct": 30.0, "kind": "owner", "basis": "your 30% target"})
    import models
    monkeypatch.setattr(models, "get_restaurant", lambda rid, *a, **k: None)
    out, status = strategy_routes._do_dish_scorecard({"restaurant_id": 1})
    assert status == 200 and [d["cost_tone"] for d in out["dishes"]] == ["good", "bad"]
    assert out["cost_reference"]["basis"] == "your 30% target"


# ── UI-11: fewer requests on open ─────────────────────────────────────────────

def test_ui11_one_count_sheet_read_a_stamped_trend_and_lazy_sections(db, monkeypatch):
    load = _between("function loadInvInsight(){", "\nfunction ")
    assert "fc2LoadCountSheet(true);fc2LoadRecipeDrafts();" in load and "fc2LoadSuppliers();" not in load
    assert load.count("loadWasteTrend();") == 1 and "loadWasteTrend(true)" not in load
    assert "if(window._fc2BoOnFile!==false){fc2LoadBo();fc2LoadPlates();}" in load
    assert "loadFcMenu(); fc2LoadPendingInvoices();" not in SRC
    assert "whenSeen('fc2-score-body'," in SRC and "if(inv&&!inv.hidden)whenSeen('fc2-invoice'," in SRC
    # The panel-open fetches, counted from the loader: about 8 for a Back
    # Office kitchen (trend, cogs, coverage, sources, count sheet, CFO,
    # purchases, plates, pars, insight) - it was ~15 with three duplicates.
    assert load.count("fc2Load") + load.count("loadWasteTrend(") + load.count("memLoadPars") <= 11
    # The stamp: the newest inventory_history write, on both payloads.
    rid = _rid(db)
    assert client_api.waste_history_stamp(rid) is None
    c = _conn(db)
    c.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end, saved_at) VALUES (?,?,?,?)",
              (rid, "{}", "2026-10-11", "2026-10-12 09:00:00"))
    c.commit()
    c.close()
    assert client_api.waste_history_stamp(rid) == "2026-10-12 09:00:00"
    payload, status = client_api._do_waste_trend(rid, "8w")
    assert status == 200 and payload["history_stamp"] == "2026-10-12 09:00:00"
    import inspect
    assert inspect.getsource(client_api.inv_insight_api).count("history_stamp=waste_history_stamp(rid)") == 2
    check = _between("function wtStampCheck(stamp){", "\n}\n")
    assert "if(stamp===undefined){loadWasteTrend(true);return;}" in check
    assert "_wt.data.history_stamp!==stamp" in check


# ── UI-12 / UI-14 / UI-15 / UI-16 / UI-17 / UI-18 ─────────────────────────────

def test_ui12_phone_width():
    assert "@media(max-width:640px){.fc2-top-right{align-items:flex-start;max-width:none;width:100%}" in SRC
    assert "@media (max-width:560px){.fc2-so-t col.q{width:110px}.fc2-so-t col.l{width:72px}" in SRC
    assert "@media (max-width:640px){.cav-itp{left:0;right:auto}}" in SRC
    tip = _between("function cavItipSet(id,on){", "\ndocument.addEventListener")
    assert "if(r.right>vw-12)dx=(vw-12)-r.right;if(r.left+dx<12)dx=12-r.left;" in tip


def test_ui14_the_per_dish_figure_is_plate_cost_pct_and_bought_vs_sold_says_what_it_is():
    assert "['cost_pct','Plate cost %','x']" in SRC and "['cost_pct','Cost %','x']" not in SRC
    fn = _between("function fc2LoadBo(){", "var FC2_PL_SHOW=")
    assert "fc2BoEsc(d.note||" in fn and 'data-itip="fc2-bo-note"' not in fn
    for rel in ("Features/FoodCost/FoodCostDishScorecard.swift", "Features/FoodCost/MenuMarginsSheet.swift"):
        src = _swift(rel)
        assert "%% food cost" not in src and 'Text("food cost")' not in src, rel
    assert 'CavnarKicker("Average plate cost %")' in _swift("Features/FoodCost/MenuMarginsSheet.swift")


def test_ui15_keyboard_and_aria():
    chart = _between("function wtDrawChart(){", "\nfunction wtTipPos")
    assert 'role="button" tabindex="0" aria-pressed="' in chart
    assert "if(k!=='Enter'&&k!==' '&&k!=='Spacebar')return;" in chart
    assert 'id="fc2-wt-ranges" role="group"' in PANEL and 'role="tablist" aria-label="Trend range"' not in PANEL
    ranges = _between("function wtDrawRanges(d){", "\n}\n")
    assert "aria-pressed=" in ranges and 'role="tab"' not in ranges
    assert "hd+='<span role=\"columnheader\" class=\"'+c[2]+'\"'+(on?' aria-sort=" in SRC
    assert "class=\"fc2-pl-head\" role=\"row\"" in SRC
    assert 'aria-label="Price per unit for {{ _fci.name }}"' in PANEL


def test_ui16_order_pills_are_light_text_on_their_tint():
    assert ".fc2-col.reduce .row .q .p{color:var(--hb-good)}" not in SRC
    assert ".fc2-col.urgent .row .q .p{background:color-mix(in srgb,var(--hb-bad) 34%,transparent);color:#ffecec}" in SRC
    assert ".fc2-col.reduce .row .q .p{background:rgba(78,173,122,.35);color:#eafaf0}" in SRC


def test_ui17_the_design_doc_matches_the_page():
    ds = open(os.path.join(ROOT, "DESIGN_SYSTEM.md"), encoding="utf-8").read()
    assert "only when both have rows" not in ds
    today = [ln for ln in ds.splitlines() if ln.startswith("| Module Today line |")][0]
    assert "Food Cost: the top CFO driver" not in today
    assert "### Food Cost fix round — the web panel and the phone (10/16/26, audit UI-01 – UI-18)" in ds


def test_ui18_the_feed_is_named_from_the_payload_and_no_schedule_is_invented():
    assert "{% set _bo_label = (_bo.label if (_bo and _bo.label) else 'Back Office') %}" in PANEL
    assert "From your {{ _bo_label }} reports." in PANEL
    assert "window._fc2BoLabel={{ _bo_label|tojson }};" in PANEL
    assert "Drafts arrive Tuesday mornings" not in SRC
    assert "load once your inventory system is connected" not in SRC
    assert "fc2FeedLabel(d)" in _between("function fc2LoadBo(){", "var FC2_PL_SHOW=")


def test_server_sentences_render_whatever_their_shape():
    js = _between("function fc2Sentences(x){", "\nfunction fc2SentenceLines") + """
console.log(JSON.stringify([fc2Sentences(null), fc2Sentences('One.'), fc2Sentences(['A.','B.']),
  fc2Sentences([{text:'T.'},{why:'W.'},{line:'L.'},{}]), fc2Sentences({sentence:'S.'})]));
"""
    assert json.loads(_node(js)) == [[], ["One."], ["A.", "B."], ["T.", "W.", "L."], ["S."]]
