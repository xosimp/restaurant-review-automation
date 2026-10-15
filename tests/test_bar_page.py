"""The Bar page (owner, 10/14/26; bar fix round 10/15/26, W8): the page script
runs under node against payloads bar_cost builds from real invoice lines and
POS sales — a complete 90 days, a missing distributor, the owner's targets,
two counts (pour cost, counted), a stock-up, a stale feed, a sales gap,
EJ's own invoices — and against edge payloads (only decreases, an all-null
trend, 500 products). What an owner sees is checked, and the rules that must
hold everywhere (never "pour cost" for a purchase ratio, focus never lost,
Reduce Motion never breaks the layout) are checked against the source too."""
import json
import os
import re
import shutil
import subprocess
from datetime import date, timedelta

import pytest

import bar_cost as bc
import fintech_invoices as fi
import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "bar_ejs_invoice_lines.json")
HEAD = ("Retailer Name,Vendor Name,Location ID,Invoice Number,Invoice Date,Invoice Total,Product Number,"
        "Product Description,U.P.C,Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n")
TODAY = date(2026, 10, 1)
ACCESS = "Food Cost isn’t part of your access — ask the owner."


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _bar_css():
    s = _src()
    a = s.index("/* ── Bar (owner, 10/14/26; redesigned")
    return s[a:s.index("</style>", a)]


def _bar_script():
    src = _src()
    start = src.index("  var OPEN = false, BACK_TO = null, FROM_DASH = false, DATA = null, GID = 0;")
    end = src.index("})();\n</script>", start)
    return "(function () {\n" + src[start:end] + "})();\n"


# A small DOM: enough for the page's own calls. Elements made by tests carry
# attributes, and focus() records itself on document.activeElement.
HARNESS = r"""
var CALLS = [], FOCUSED = [];
function el(attrs){var a=attrs||{};var e={innerHTML:'',textContent:'',style:{},hidden:true,disabled:false,value:a.value||'',
  classList:{add:function(){},remove:function(){},toggle:function(){}},
  setAttribute:function(k,v){a[k]=v;},removeAttribute:function(k){delete a[k];},hasAttribute:function(k){return k in a;},
  getAttribute:function(k){return k in a ? a[k] : null;},addEventListener:function(){},appendChild:function(){},
  querySelector:function(){return null;},querySelectorAll:function(){return [];},contains:function(){return true;},
  focus:function(){FOCUSED.push(e);document.activeElement=e;}};return e;}
var ELS = {};
var document={hidden:false,body:el(),activeElement:null,getElementById:function(id){return ELS[id]||null;},createElement:function(){return el();},
  querySelector:function(){return null;},querySelectorAll:function(){return [];},addEventListener:function(){}};
var window={location:{pathname:'/'},history:{pushState:function(){},replaceState:function(){},back:function(){}},innerWidth:1200,
  matchMedia:function(){return {matches:true};}};
var history=window.history;
var NEXT = {ok:true};
function fetch(url, o){CALLS.push({url:url, method:(o&&o.method)||'GET', body:o&&o.body?JSON.parse(o.body):null});
  return {then:function(f){var r={ok:true,status:200,json:function(){return {then:function(g){var x=g(NEXT);return {then:function(h){h(x);}};}};}};
    var v=f(r);return {then:function(h){if(v&&v.then){v.then(h);}else{h(v);}}};}};}
"""


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _run(payload, tail, pre=""):
    """The script with `payload` rendered; `tail` is JS run after, printing."""
    return _node(HARNESS + pre + _bar_script() + "var HTML = window._barRender(%s);\n" % json.dumps(payload, default=str) + tail)


def _render(payload, pre=""):
    return _run(payload, "console.log(HTML);", pre)


def _text(html):
    t = re.sub(r"<svg.*?</svg>", "", html, flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", " ", t.replace("&amp;", "&"))


def _row(vendor, inv, day, code, desc, uom, qty, cost):
    return f'EJs,"{vendor}",1,{inv},{day},${qty * cost:.2f},{code},{desc},0,{uom},{qty},${cost:.2f},$0.00\n'


def _metric(conn, rid, day, metric, value):
    conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                 "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, day, metric, value))


def _world(db, days=90, today=TODAY, liquor_gap=False, stock_up=False, sales_gap=False, **targets):
    """A bar with weekly beer and liquor deliveries, wine every other week, and
    a POS that sold every night; options knock one piece out."""
    rid = models.create_restaurant(models.Restaurant(name="EJ's", owner_email="o@x.test"), db_path=db)
    if targets:
        models.update_restaurant(rid, targets, db_path=db)
    start = today - timedelta(days=days)
    weeks = days // 7
    csv = HEAD
    for w in range(weeks):
        ds = (start + timedelta(days=7 * w + 1)).strftime("%m/%d/%Y")
        csv += _row("Chicago Beverage", f"C{w}", ds, "900", "DAISY CUTTER K 1/2BB", "KE", 4, 229.0 if w < 6 else 239.0)
        if not (liquor_gap and weeks - 3 <= w < weeks - 1):
            csv += _row("Breakthru Beverage", f"B{w}", ds, "101", "TITOS HANDMADE VODKA 1L", "CA", 3, 250.0 if w < 6 else 262.0)
        if w % 2 == 0:
            csv += _row("Wine House", f"W{w}", ds, "301", "WHISPER ANGEL ROSE 750ML", "CA", 2, 240.0)
    if stock_up:
        csv += _row("Breakthru Beverage", "BIG", (today - timedelta(days=5)).strftime("%m/%d/%Y"), "101",
                    "TITOS HANDMADE VODKA 1L", "CA", 40, 262.0)
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    conn = models.get_conn(db)
    for n in range(days):
        d = (start + timedelta(days=n)).isoformat()
        if sales_gap and n == days - 20:
            _metric(conn, rid, d, "sales.net", 5000)
            _metric(conn, rid, d, "sales.cat:Unmapped", 5000)
            continue
        for cat, v in (("Beer", 600), ("Liquor", 1000), ("Wine", 220)):
            _metric(conn, rid, d, "sales.cat:" + cat, v)
    conn.commit()
    conn.close()
    return rid


def _build(db, today=TODAY, **kw):
    rid = _world(db, today=kw.pop("world_today", today), **kw)
    return bc.build(rid, db_path=db, today=today)


# ── the rules that must hold on every payload ────────────────────────────

ALLOWED_POUR = re.compile(r"counted[ -]pour[ -]cost|pour cost,? counted|pour cost \(counted\)|Pour cost needs two counts"
                          r"|becomes pour cost|pour cost once", re.I)


def _no_pour_cost_for_a_purchase_ratio(html):
    """V1: "pour cost" only names a counted figure, the range for one, or the
    explanation of what it needs — never the purchase ratio."""
    # The explanation panel closes the hero's left column (10/15/26: it moved
    # behind "What this measures", under the figures).
    explain = re.search(r'data-br-explain="1"[^>]*>(.*?)</div></div>\s*<div class="br-hr"', html, re.S)
    body = html.replace(explain.group(1), "") if explain else html
    text = _text(body)
    for m in re.finditer(r"pour[ -]cost", text, re.I):
        window = text[max(0, m.start() - 12):m.end() + 12]
        assert ALLOWED_POUR.search(window), window


def _common(html):
    for tag in re.findall(r"<button[^>]*>", html):
        assert "cbtn" in tag, tag
    for tag in re.findall(r"<select[^>]*>", html):
        assert "br-sel" in tag, tag                       # V48: every select is the branded one
    assert "−0.0" not in html and "-0.0 pts" not in html  # V44
    assert 'class="br-cat row"' not in html                 # UIX-19 double padding
    # Collapsed rows hold nothing focusable (V30): their detail is built on first open.
    for row in re.findall(r'<article class="br-card br-row(?! open).*?</article>', html, re.S):
        rb = row[row.index('class="br-rb"'):]
        assert "inert" in rb[:40]
        assert not re.search(r"<(select|input|button|a)\b", rb), rb[:200]


@pytest.fixture
def full(db_path):
    return _build(db_path)


def test_a_complete_ninety_days_reads_bought_vs_sold_with_its_basis_chip(full):
    pc = full["pour_cost"]
    assert pc["basis"] == "purchases" and pc["judged"], pc.get("not_judged")
    html = _render(full)
    _common(html)
    _no_pour_cost_for_a_purchase_ratio(html)
    hero = html[html.index('class="br-card br-hero'):html.index('class="br-cats"')]
    assert "Bought vs sold · " in hero
    assert "Purchase-based · not a shelf count" in hero and 'data-br-why="1"' in hero
    assert "Pour cost needs two counts" in hero and 'data-br-go="br-counts"' in hero   # the disclosure, and how
    # The range is context: a neutral pill, a dashed band, no green zone, no red.
    assert "the counted-pour-cost range" in hero and 'class="br-pill neutral"' in hero
    assert 'class="br-meter ctx"' in hero and "--hb-good" not in hero.split('class="br-meter')[1][:400]
    assert "Set an alcohol target in Account → Targets" in hero and 'data-br-targets="1"' in hero
    for piece in ("Bought vs sold, week by week", "Week by week, as a table", "Month by month, as a table",
                  "What your distributors charge", "Where the money goes", "Which POS sales count as bar sales",
                  "How every product is sorted", "Count the bar", 'data-br-series="total"'):
        assert piece in html, piece
    # Sales say their basis under the figures.
    assert "Sales: " in hero and "after comps and check discounts" in hero


def test_the_weekly_values_are_a_table_and_the_chart_takes_the_keyboard(full):
    html = _render(full)
    weeks = html[html.index('id="br-weeks"'):]
    weeks = weeks[:weeks.index("</details>")]
    assert weeks.count("<tr>") - 1 == len(full["pour_cost"]["trend"])
    box = re.search(r'<div class="br-chart br-tc"[^>]*>', html).group(0)
    assert 'tabindex="0"' in box and "arrows" in box
    assert 'id="br-tlive" aria-live="polite"' in html


def test_price_watch_opens_on_what_went_up_and_held_biggest_money_first(full):
    pw = full["price_watch"]
    assert {r["name"] for r in pw["increases"]} >= {"Titos Handmade Vodka 1L"}
    html = _render(full)
    rows = re.findall(r'data-br-row="([^"]+)"', html)
    assert rows and len(rows) == len(pw["increases"])
    assert 'data-br-dir="up" aria-pressed="true"' in html
    assert "at your usual buying" in html and "not a bill already paid" in html
    _common(html)


def test_a_missing_distributor_names_it_and_offers_we_stopped_buying(db_path):
    p = _build(db_path, liquor_gap=True)
    assert p["pour_cost"]["gaps"], "the gap detector should see Breakthru's missing weeks"
    html = _render(p)
    assert "Some invoices aren’t on file yet." in html
    assert 'data-br-stop="Breakthru Beverage"' in html and "We stopped buying from them" in html
    assert "(Liquor)" in html, "categories by their labels, never keys"
    no = _render(p, pre="window._BAR_OWNER = false;")
    assert "data-br-stop" not in no and "data-br-cat" not in no


def _card(html, label):
    m = re.search(r'<article class="br-card lift br-cat[^"]*"[^>]*aria-label="' + re.escape(label.replace("&", "&amp;")) + r'">.*?</article>',
                  html, re.S)
    assert m, label
    return m.group(0)


def test_the_page_renders_a_gap_with_its_reason_and_no_judgement(db_path):
    """EDG list 44: a category with a house's invoices missing shows what's
    in so far with the reason, never a verdict pill or a dollars-over line."""
    p = _build(db_path, liquor_gap=True)
    liq = next(c for c in p["pour_cost"]["categories"] if c["key"] == "liquor")
    assert not liq["judged"] and liq["not_judged_text"], liq
    card = _card(_render(p), "Liquor")
    assert 'class="na"' in card and ("Not judged, for the reason above." in card
                                     or _text(liq["not_judged_text"])[:20] in _text(card))
    assert "br-ov" not in card and "br-pill bad" not in card and "br-pill good" not in card


def test_the_page_draws_the_trend_once_four_weeks_fit(full, db_path):
    """EDG list 46: ninety days draw the hero's week-by-week chart and each
    category's sparkline; under four weeks there is no chart, the thin-data
    sentence instead."""
    html = _render(full)
    assert 'data-br-trend="1"' in html and "<svg" in html[html.index('data-br-trend="1"'):][:4000]
    for label in ("Beer & seltzer", "Liquor", "Wine"):
        assert '<svg' in _card(html, label), label
    short = bc.build(_world(db_path, days=20), db_path=db_path, today=TODAY)
    sh = _render(short)
    assert 'data-br-trend="1"' not in sh and "fills in as four-week windows" in sh


def test_the_page_never_says_four_weeks_for_a_shorter_window(db_path):
    """EDG list 47 / V8: a 20-day overlap is shown as what it is — its own
    dates and "N of 28 days" — never as "the last four weeks"."""
    rid = _world(db_path, days=20)
    # The day after the last delivery: a fresh feed, so the short window is the reason.
    p = bc.build(rid, db_path=db_path, today=TODAY - timedelta(days=11))
    w = p["pour_cost"]["window"]
    assert w["days"] < 28 and p["pour_cost"]["total"]["not_judged"] == "short_window", p["pour_cost"]["total"]
    html = _render(p)
    hero = _text(html[html.index('class="br-card br-hero'):html.index('class="br-cats"')])
    assert f"{w['days']} of 28 days" in hero and w["from_mdy"] in hero and w["to_mdy"] in hero
    text = _text(html)
    for claim in ("over the last four weeks", "in these four weeks", "over four weeks ·", "in four weeks",
                  "the four weeks above"):
        assert claim not in text, claim
    assert f"the {w['days']} days above" in text
    _common(html)


def test_spend_sections_state_their_period_and_the_hero_its_own(full):
    """V46: the distributors' and the products' spend are every invoice on
    file — each says its dates — while the hero's "bought" is the window's."""
    inv = full["invoices"]
    html = _render(full)
    assert f"Distributors · {inv['from_mdy']} – {inv['to_mdy']}" in _text(html)
    hero = _text(html[html.index('class="br-card br-hero'):html.index('class="br-cats"')])
    w = full["pour_cost"]["window"]
    assert f"{w['from_mdy']} – {w['to_mdy']}" in hero and "bought from distributors" in hero
    out = _run(full, "window._bar.setData(%s); console.log(window._bar.prodBody());" % json.dumps(full, default=str))
    assert f"Spend is every invoice on file, {inv['from_mdy']} – {inv['to_mdy']}." in _text(out)


def test_the_page_renders_the_no_overlap_state(full):
    """EDG list 48: invoices on file but no sales overlapping them: the page
    says so and draws no figure."""
    p = json.loads(json.dumps(full))
    p["pour_cost"] = {"available": False, "basis": "purchases",
                      "why_not": "The invoices and the sales on file don't overlap yet."}
    html = _render(p)
    assert "don’t overlap yet" in html or "don't overlap yet" in html
    assert 'class="br-big"' not in html
    _common(html)


def test_the_owners_target_judges_and_the_tick_is_labelled_your(db_path):
    p = _build(db_path, bar_cost_target=15, liquor_cost_target=20)
    t = p["pour_cost"]["total"]
    assert t["judged"] and t["target"] == 15
    html = _render(p)
    hero = html[html.index('class="br-card br-hero'):html.index('class="br-cats"')]
    assert re.search(r"(Over|Under) your <span class=\"hb-num\">15%</span> target", hero) or "your 15% target" in _text(hero)
    assert 'class="tgl"' in hero and "Your " in hero
    assert "Set an alcohol target" not in hero
    liquor = html[html.index('aria-label="Liquor"'):]
    liquor = liquor[:liquor.index("</article>")]
    assert "your 20% target" in _text(liquor)
    ovs = re.findall(r'<div class="br-ov[^"]*">(.*?)</div>', html, re.S)
    assert ovs, "a target the bar is over draws its dollars line"
    for ov in ovs:
        assert "more bought than" in _text(ov), ov            # never a bare "$X over"
    _common(html)


def test_two_counts_make_the_hero_pour_cost_counted(db_path):
    import bar.counts as bcn
    rid = _world(db_path)
    win = bc.build(rid, db_path=db_path, today=TODAY)["pour_cost"]["window"]
    bcn.save_count(rid, win["from"], {"beer": 4200, "liquor": 9100, "wine": 2600}, db_path=db_path, today=TODAY)
    bcn.save_count(rid, win["to"], {"beer": 3900, "liquor": 8700, "wine": 2500}, db_path=db_path, today=TODAY)
    p = bc.build(rid, db_path=db_path, today=TODAY)
    assert p["pour_cost"]["basis"] == "counted"
    html = _render(p)
    hero = html[html.index('class="br-card br-hero'):html.index('class="br-cats"')]
    assert "Pour cost (counted) · " in hero and 'class="br-bchip counted"' in hero
    assert "used · opening + bought − closing" in hero
    assert 'class="br-card br-eq' in html and "pour cost, counted" in html
    assert "Purchase-based" not in hero
    _common(html)


def test_a_stock_up_is_named_and_not_judged_no_red_or_green(db_path):
    p = _build(db_path, stock_up=True)
    liq = next(c for c in p["pour_cost"]["categories"] if c["key"] == "liquor")
    assert liq["stock_up"] and not liq["judged"]
    html = _render(p)
    card = html[html.index('aria-label="Liquor"'):]
    card = card[:card.index("</article>")]
    assert "Not judged yet" in card and "Large delivery" in card
    assert "--hb-bad" not in card and "--hb-good" not in card.split("br-meter")[0]
    said = liq["not_judged_text"].split(" — ")[0][:30]
    if liq["not_judged_text"] == p["pour_cost"]["total"].get("not_judged_text"):
        assert "Not judged, for the reason above." in card and said in _text(html)   # said once, in the hero
    else:
        assert said in _text(card)
    _no_pour_cost_for_a_purchase_ratio(html)


def test_a_stale_feed_says_how_far_behind_the_invoices_are(db_path):
    rid = _world(db_path)
    p = bc.build(rid, db_path=db_path, today=TODAY + timedelta(days=45))
    fr = p["pour_cost"].get("freshness") or {}
    assert fr.get("stale") or (p.get("feed") or {}).get("stale_days"), fr
    html = _render(p)
    assert "Invoices are " in html and "behind." in html and "Fintech" in html
    # V22: the header says how far sales run beside how far invoices run.
    head = re.search(r"'invoices through ' \+ [^;]*;", _src()).group(0)
    assert "sales_through_mdy" in head and "' · sales through '" in head


def test_held_invoices_are_named_future_and_years_old(full):
    """V35: both holds are listed with the invoice and its date."""
    p = json.loads(json.dumps(full))
    p["held_future"] = [{"distributor": "Breakthru", "invoice_number": "TYPO", "date": "2027-10-02",
                         "date_mdy": "10/2/27"}]
    p["held_old"] = [{"distributor": "Southern Glazer's", "invoice_number": "OLD", "date": "2016-10-06",
                      "date_mdy": "10/6/16"}]
    html = _text(_render(p))
    assert "dated after today" in html and "#TYPO" in html and "10/2/27" in html
    assert "two years before you joined Cavnar AI" in html and "#OLD" in html and "10/6/16" in html


def test_a_sales_gap_night_is_named_with_the_way_to_fix_it(db_path):
    p = _build(db_path, sales_gap=True)
    assert p["pour_cost"].get("sales_gaps"), p["sales_basis"]
    html = _render(p)
    assert "drink sales aren’t sorted into beer, liquor and wine" in html
    assert 'data-br-go="br-mapsec"' in html


def test_mapping_needed_opens_the_mapping_and_brunch_drinks_can_move_to_liquor(full):
    p = json.loads(json.dumps(full, default=str))
    p["sales_basis"].update({"mapping_needed": True, "unmapped_say": True, "unmapped_usd": 2443.25, "unmapped_pct": 2.9,
                             "unmapped_categories": [{"key": "Beverage › 20. Beverages", "kind": "item_category",
                                                      "sales_usd": 2443.25, "why": "its name doesn't say"}]})
    mapping = {"ok": True, "can_change": True, "mapping_needed": True,
               "options": [{"key": k, "label": l} for k, l in (("beer", "Beer & seltzer"), ("liquor", "Liquor & cocktails"),
                                                               ("wine", "Wine"), ("na", "Non-alcoholic"), ("not_bar", "Not bar"))],
               "entries": [{"key": "Beer › 56. Brunch Drinks", "kind": "item_category", "label": "Beer › 56. Brunch Drinks",
                            "choice": "beer", "choice_label": "Beer & seltzer", "source": "suggested", "suggestion": "beer",
                            "suggestion_why": "its department name reads beer", "sales_28d": 1840.0, "needs_you": True,
                            "confirmed": False}]}
    html = _render(p)
    assert "$2,443" in _text(html) and "Sort your POS categories" in html
    assert 'id="br-mapd" data-br-lazy="mapping" open' in html
    out = _run(p, """
      window._bar.state.map = %s;
      var body = window._bar.mapBody();
      console.log(body.indexOf('value="liquor"') >= 0 && body.indexOf('data-br-map="Beer › 56. Brunch Drinks"') >= 0 ? 'OPTION' : 'NO');
      var sel = el({'data-br-map': 'Beer › 56. Brunch Drinks', 'data-br-kind': 'item_category', value: 'liquor'});
      sel.value = 'liquor'; sel.options = [{text: 'Liquor & cocktails'}]; sel.selectedIndex = 0;
      window._bar.saveMapping(sel);
      console.log(JSON.stringify(CALLS));
    """ % json.dumps(mapping))
    assert "OPTION" in out
    call = json.loads(out.strip().split("\n")[-1])[0]
    assert call["url"] == "/api/food-cost/bar/mapping" and call["method"] == "POST"
    assert call["body"]["entries"] == [{"key": "Beer › 56. Brunch Drinks", "kind": "item_category", "bar_category": "liquor"}]


def test_only_decreases_opens_on_all_and_up_names_what_came_down(full):
    p = json.loads(json.dumps(full, default=str))
    dn = dict(p["price_watch"]["increases"][0], kind="decrease", change=-12.0, pct=-4.6, forward_monthly_usd=-36)
    p["price_watch"]["increases"] = []
    p["price_watch"]["decreases"] = [dn]
    html = _render(p)
    assert 'data-br-dir="all" aria-pressed="true"' in html and 'data-br-row="' in html
    assert 'id="br-live" aria-live="polite"' in html                      # V48: a polite live count
    assert re.search(r"@media \(pointer:coarse\)\{[^}]*\.br-seg \.cbtn[^}]*min-height:44px", _bar_css())
    out = _run(p, "window._bar.state.dir='up'; console.log(window._bar.listHtml());")
    assert "No price went up and held" in out and "came down" in out and 'data-br-dir="down"' in out


def test_an_all_null_trend_says_why_instead_of_drawing_empty_axes(full):
    p = json.loads(json.dumps(full, default=str))
    for pt in p["pour_cost"]["trend"]:
        for k in ("total", "beer", "liquor", "wine"):
            pt[k] = None
    p["pour_cost"]["total"]["spark"] = [None] * len(p["pour_cost"]["trend"])
    html = _render(p)
    assert "br-hc" not in html and "data-br-trend" not in html
    assert "measured so far" in html


def test_an_unchanged_reading_is_never_a_red_drop(full):
    p = json.loads(json.dumps(full, default=str))
    p["pour_cost"]["total"]["prev_pct"] = p["pour_cost"]["total"]["pct"]
    html = _render(p)
    hero = html[html.index('class="br-card br-hero'):html.index('class="br-cats"')]
    assert "−0.0" not in html and "br-delta bad" not in html and "unchanged" in hero


def test_five_hundred_products_search_stays_small_and_rows_build_on_open(full):
    p = json.loads(json.dumps(full, default=str))
    base = p["price_watch"]["increases"][0]
    rows = []
    for i in range(500):
        r = dict(base, product_key=f"x|{i}", name=f"Vodka number {i} " + "x" * 40, product=f"VODKA {i}",
                 history=base["history"] * 3)
        rows.append(r)
    p["price_watch"]["increases"] = rows[:334]
    p["price_watch"]["decreases"] = [dict(r, kind="decrease", change=-3) for r in rows[334:]]
    p["products"] = [dict(p["products"][0], product_key=f"x|{i}", product=f"VODKA {i}") for i in range(500)]
    html = _render(p)
    assert html.count("<select") < 12, "the product table and row details are built when opened"
    out = _run(p, "window._bar.state.q='v'; window._bar.state.noRemote=true; var h=window._bar.listHtml(); console.log(h.length); console.log(h);")
    size = int(out.split("\n")[0])
    assert size < 150_000, size
    # R15: a keystroke never rebuilds the list on its own — both searches wait 150 ms.
    js = _bar_script()
    assert "if (QT) clearTimeout(QT);" in js and "QT = setTimeout(function () { QT = null; refreshList(); }, 150);" in js
    assert "if (PT) clearTimeout(PT);" in js and "PT = setTimeout(function () { PT = null;" in js
    _common(out)


def test_a_long_name_wraps_to_two_lines_and_the_detail_says_it_in_full(full):
    p = json.loads(json.dumps(full, default=str))
    long = "Woodford Reserve Kentucky Straight Bourbon Whiskey Distillers Select Barrel Proof 1.75L"
    p["price_watch"]["increases"][0]["name"] = long
    html = _render(p)
    assert f'title="{long}"' in html
    css = _bar_css()
    assert "-webkit-line-clamp:2" in css[css.index(".br-nm b{"):][:200]
    out = _run(p, "console.log(window._bar.rowDetail(%s));" % json.dumps(p["price_watch"]["increases"][0]))
    assert 'class="br-full">' + long in out          # the detail's own heading, not a control's label


def test_deal_endings_and_order_sizes_are_named_never_increases(db_path):
    """EJ's own invoices (5/4–10/13/26): Son Cutrer's $174 is its usual price,
    a $156 deal ended; it shows as a deal, not a red increase."""
    fx = json.load(open(FIXTURE))
    rid = models.create_restaurant(models.Restaurant(name="EJ's", owner_email="o@x.test"), db_path=db_path)
    conn = models.get_conn(db_path)
    inv = {}
    for n, r in enumerate(dict(zip(fx["columns"], row)) for row in fx["rows"]):
        k = (r["distributor"], r["invoice_number"])
        if k not in inv:
            inv[k] = conn.execute(
                "INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, total, "
                "source_format, balanced, updated_at) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
                (rid, r["distributor"], r["invoice_number"], r["invoice_date"], 0, "export", 1, "2026-10-14 00:00:00")).fetchone()[0]
        conn.execute("INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, upc, uom, qty, "
                     "unit_price, cost, deposit, adjustment, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (inv[k], n + 1, r["item_code"], r["description"], r["upc"], r["uom"], r["qty"], r["unit_price"],
                      r["cost"], r["deposit"], r["adjustment"], r["kind"]))
    d = date(2026, 7, 1)
    while d <= date(2026, 10, 13):
        for cat, v in (("Beer", 2100), ("Liquor", 1800), ("Wine", 320)):
            _metric(conn, rid, d.isoformat(), "sales.cat:" + cat, v)
        d += timedelta(days=1)
    conn.commit()
    conn.close()
    p = bc.build(rid, db_path=db_path, today=date(2026, 10, 14))
    out = _run(p, "window._bar.state.dir='deals'; console.log(window._bar.listHtml());")
    son = out[out.index("SON Cutrer"):]
    son = son[:son.index("</article>")]
    assert "Deal ended" in son and "not a price change" in son and "M8 13V3" not in son
    html = _render(p)
    _common(html)
    _no_pour_cost_for_a_purchase_ratio(html)


def test_variance_waste_keg_yield_and_comps_render_from_the_payload(full):
    p = json.loads(json.dumps(full, default=str))
    p["variance"] = {"available": True, "basis": "bought_vs_rung", "label": "Bought vs rung",
                     "span": {"from_mdy": "9/1/26", "to_mdy": "10/1/26"},
                     "items": [{"product_key": "k|1", "product": "Miller Lite 24/12 CAN", "category": "beer", "unit": "each",
                                "unit_word": "can", "bought_units": 48, "rung_units": 40, "comped_units": 1, "gap_units": 8,
                                "gap_usd": 16.0, "theoretical_basis": ["one_each"], "drinks": ["Miller Lite Btl"],
                                "confidence": "high", "assumes": [], "waste_units": 3, "waste_entries": 1}],
                     "items_total": 1, "unmapped": [{"provider": "rpower", "item_id": "B9", "item": "Bud Light Btl",
                                                     "category": "beer", "rung_units": 10, "sales_usd": 50.0, "spec_suggested": False}],
                     "unmapped_total": 1, "unmapped_sales_usd": 50.0, "totals": {"gap_usd": 16.0, "comped_usd": 2.0},
                     "suggestions": [{"id": 1, "provider": "rpower", "item_name": "Espresso Martini", "item_id": "E1",
                                      "ingredient": "Ketel One Vodka", "product_key": "k|9", "oz": 1.5, "status": "suggested"}],
                     "suggestions_total": 1, "note": "Comps are poured, not paid: shown apart, never counted as rung."}
    p["bar_ops"] = {"waste": {"entries": 1, "by_reason": [{"reason": "breakage", "label": "Breakage", "entries": 1}],
                              "recent": [{"id": 7, "waste_date_mdy": "9/6/26", "product": "Miller Lite 24/12 CAN", "qty": 3,
                                          "unit": "each", "reason_label": "Breakage"}],
                              "note": "Logged waste is shown beside the gap, never taken out of it."},
                    "keg_yield": [{"product": "Miller Lite 1/2 BBL", "keg_label": "1/2 barrel", "kegs_bought": 2, "pints_rung": 200,
                                   "pints_per_keg": 100, "theoretical_pints_per_keg": 124, "yield_pct": 80.6,
                                   "expected_yield_pct": 90, "assumes": ["every keg bought in the span was poured in it"]}],
                    "comps": {"owner_only": True}}
    html = _render(p)
    text = _text(html)
    assert "Bought vs rung" in text and "Bud Light Btl" in text and "never counted as zero" in text
    assert "poured, not paid" in text and 'data-br-vmap="B9"' in html
    assert 'data-br-spec="1" data-act="confirm"' in html
    assert "80.6%" in text and "Assumes every keg" in text
    assert 'data-br-wlog="1"' in html and 'data-br-wdel="7"' in html and "3 bottles or cans" in text
    assert "Comps &amp; voids" not in html, "owner-only: nothing for anyone else"
    p["bar_ops"]["comps"] = {"owner_only": False, "available": True, "span": {"from_mdy": "8/11/26", "to_mdy": "10/5/26"},
                             "basis_text": "the bar's tickets", "totals": {"sales_usd": 14000, "comp_usd": 1050, "void_usd": 0,
                                                                          "rate_pct": 7.5},
                             "shifts": {"available": True, "min_shifts": 6, "baseline_rate_pct": 7.5,
                                        "by_bartender": [{"bartender": "Alex", "shifts": 14, "sales_usd": 7000, "comp_usd": 1050,
                                                          "void_usd": 0, "rate_pct": 15.0}]},
                             "flagged": [{"bartender": "Alex", "headline": "Comps ran higher on the 14 shifts Alex was scheduled",
                                          "alternative": "A busy promo night can do this."}],
                             "note": "A pattern worth reviewing, not a finding of wrongdoing."}
    comps = _render(p)
    c = comps[comps.index('id="br-comps"'):]
    c = c[:c.index("</table>")]
    assert "not a finding of wrongdoing" in _text(comps) and "--hb-bad" not in c and " bad" not in c


def test_a_refusal_says_whose_call_it_is_and_failures_say_try_again():
    assert ACCESS in _render({"ok": False, "http_status": 403, "module_forbidden": True})
    assert "couldn’t be read" in _render({"ok": False, "http_status": 500})
    assert "waiting on invoices" in _render({"ok": True, "available": False, "why_not": "No invoices."})
    out = _node(HARNESS + _bar_script() + "console.log(window._bar.refusal({ok:false, http_status:403, module_forbidden:true, error:'x'}));"
                "console.log(window._bar.refusal({ok:false, http_status:403, owner_only:true, error:'Only the account owner can change the bar mapping.'}));")
    assert ACCESS in out and "Only the account owner" in out


def test_focus_survives_a_save_and_show_all_focuses_the_first_new_row(full):
    out = _run(full, """
      var sel = el({'data-br-cat': 'k|1'}); document.activeElement = sel;
      var found = el({'data-br-cat': 'k|1'});
      var body = el(); body.querySelector = function (q) { return q === '[data-br-cat="k|1"]' ? found : null; };
      ELS['br-body'] = body; ELS['bar'] = el(); ELS['br-scroll'] = el();
      window._bar.open(true); window._bar.rerender(%s);
      console.log(document.activeElement === found ? 'KEPT' : 'LOST');
      var heads = []; for (var i = 0; i < 20; i++) heads.push(el({'data-br-row': 'r' + i}));
      var list = el(); list.querySelectorAll = function () { return heads; }; ELS['br-list'] = list;
      window._bar.redrawList(8);
      console.log(document.activeElement === heads[8] ? 'ROW8' : 'NOT8');
    """ % json.dumps(full, default=str))
    assert "KEPT" in out and "ROW8" in out


def test_writes_send_what_the_routes_take(full):
    out = _run(full, """
      ELS['br-cdate'] = el({value: '2026-09-30'}); ELS['br-cdate'].value = '2026-09-30';
      var inp = el({'data-br-cnt': 'liquor'}); inp.value = '$1,200';
      window._bar.saveCount(inp);
      window._bar.setStopped('Breakthru Beverage', true);
      window._bar.state.q = 'vod'; window._bar.state.dir = 'deals'; window._bar.state.cat = 'liquor';
      console.log(JSON.stringify(CALLS)); console.log(window._bar.priceUrl(50));
    """)
    lines = out.strip().split("\n")
    calls = [c for c in json.loads(lines[-2]) if c["method"] != "GET"]
    assert calls[0]["url"] == "/api/food-cost/bar/counts" and calls[0]["body"] == {"count_date": "2026-09-30", "values": {"liquor": "$1,200"}}
    assert calls[1]["url"] == "/api/food-cost/bar/stopped" and calls[1]["body"] == {"distributor": "Breakthru Beverage"}
    url = lines[-1]
    assert url.startswith("/api/food-cost/bar/prices?offset=50&limit=50") and "q=vod" in url and "kind=deal_ended,deal,tier" in url
    assert "cat=liquor" in url and "sort=money" in url


def test_narrow_charts_print_three_dates_and_the_tip_stays_inside():
    out = _node(HARNESS + _bar_script() + """
      console.log(JSON.stringify([window._bar.labelIdx(12, true), window._bar.labelIdx(12, false).length,
                                  window._bar.tipLeft(251, 200, 240), window._bar.tipLeft(251, 200, 10)]));
    """)
    narrow, wide, a, b = json.loads(out)
    assert len(narrow) <= 3 and narrow[0] == 0 and narrow[-1] == 11 and wide == 6
    assert 0 <= a <= 51 and 0 <= b <= 51


# ── the CSS rules (V16, V30, V31, V48, V49) ─────────────────────────────

def _media(css, head):
    i = css.index(head)
    depth, j = 0, css.index("{", i)
    for k in range(j, len(css)):
        depth += {"{": 1, "}": -1}.get(css[k], 0)
        if depth == 0:
            return css[j + 1:k]
    raise AssertionError(head)


def _rules(block):
    return re.findall(r"([^{}]+)\{([^{}]*)\}", block)


def test_reduce_motion_stops_the_entrances_and_never_resizes_a_card():
    block = _media(_bar_css(), "@media (prefers-reduced-motion:reduce)")
    rise = [(s, d) for s, d in _rules(block) if ".br-rise" in s]
    assert rise, block
    for sel, decl in rise:
        assert "animation:none" in decl.replace(" ", "")
        assert not re.search(r"(^|;)\s*(width|height|margin)\s*:", decl), (sel, decl)
    # No rule in the block resizes anything (V16 merged the dot sizing into it).
    for sel, decl in _rules(block):
        assert not re.search(r"(^|;)\s*(width|height|margin|padding)\s*:", decl), (sel, decl)
    css = _bar_css()
    assert ".br-chart .dot.sm{width:7px;height:7px" in css


def test_keyboard_focus_is_visible_on_rows_segments_and_the_chart():
    css = _bar_css()
    for sel in (".br-row .cbtn.br-rh:focus-visible", ".br-seg .cbtn:focus-visible", ".br-chart.br-tc:focus-visible"):
        m = re.search(re.escape(sel) + r"[^{]*\{([^}]*)\}", css)
        assert m, sel
        assert re.search(r"(box-shadow|outline):(?!none)", m.group(1)), (sel, m.group(1))
    assert "inset 0 0 0 2px" in re.search(re.escape(".br-row .cbtn.br-rh:focus-visible") + r"\{([^}]*)\}", css).group(1)


def _hex(h):
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def _lum(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_small_red_text_clears_four_and_a_half_to_one_on_the_dark_surface():
    s = _src()
    dark = s[s.index('[data-theme="dark"]{'):]
    tok = {k: re.search(r"--" + k + r":\s*(#[0-9a-fA-F]{6})", dark).group(1) for k in ("red", "ink", "surface")}
    m = re.search(r"--br-red-text:color-mix\(in srgb,var\(--red\) (\d+)%,var\(--ink\)\)", _bar_css())
    assert m, "the text-safe red is red mixed into the ink"
    w = int(m.group(1)) / 100
    mix = [w * a + (1 - w) * b for a, b in zip(_hex(tok["red"]), _hex(tok["ink"]))]
    ratio = (_lum(mix) + 0.05) / (_lum(_hex(tok["surface"])) + 0.05)
    assert ratio >= 4.5, ratio
    css = _bar_css()
    for sel in (".br-delta.bad", ".br-ov.bad", ".br-li .r.bad", ".br-err"):
        assert re.search(re.escape(sel) + r"\{[^}]*color:var\(--br-red-text\)", css), sel
    # The register's other two: a price row's change figure and a bad insight's kicker.
    js = _bar_script()
    assert "increase: {c: 'var(--br-up)', ct: 'var(--br-red-text)'" in js
    assert "(x.tone === 'bad' ? ';--ct:var(--br-red-text)' : '')" in js


def test_print_keeps_the_bar_page():
    block = _media(_bar_css(), "@media print")
    assert '#bar[role="dialog"]' in block and "display:block!important" in block
    # Only while the Bar is the open page (the DNA page sets dn-open too).
    assert "body.dn-open:has(>#bar:not([hidden]))>*:not(#bar){display:none!important}" in block
    # The global print block hides every .cbtn; a Price watch row's header is
    # one, so the Bar's block prints it (V49: rows printed empty otherwise).
    assert "#bar .br-row .cbtn.br-rh{display:grid!important" in block
    glob = _media(_src(), "@media print{\n  @page")
    assert ".cbtn" in glob.split("{display:none!important}")[0]


def test_motion_tokens_and_the_draw_in():
    css = _bar_css()
    assert "#bar{--hb-line:" in css and "--hb-good:var(--green)" in css
    assert "stroke-dasharray" not in css, "a dash draw-in does not survive non-scaling strokes"
    assert "#bar.br-still .br-rise" in css, "a re-read never replays the entrance"


def test_food_bar_is_a_link_not_a_tab_and_the_app_is_inert_behind_the_page():
    s = _src()
    nav = s[s.index('<nav class="br-fsw'):]
    nav = nav[:nav.index("</nav>")]
    assert 'role="tab' not in nav and '<a href="/bar" data-bar-open="1">Bar</a>' in nav
    js = _bar_script()
    assert "inertApp(true)" in js and "inertApp(false)" in js and "k.id === 'toast'" in js


def test_account_targets_say_the_error_under_the_row_and_keep_the_typed_value():
    s = _src()
    for f in ("bar_cost_target", "beer_cost_target", "liquor_cost_target", "wine_cost_target"):
        tag = re.search(r'<input[^>]*id="as-tg-' + f + '"[^>]*>', s).group(0)
        assert 'placeholder="Not set"' in tag, f
    h = s[s.index("var f = t.getAttribute('data-tg'), role = t.getAttribute('data-tg-role')"):]
    h = h[:h.index("/* ── Opening Labor")]
    assert "tgFieldErr(t, tgLabel(t) + ' must be between ' + lo + tgUnit(t)" in h
    assert "else if (f) { tgFieldErr(t, d.error" in h, "a refused target keeps its typed value (no tgRender)"
    assert "Blank uses the industry 16" not in s, "the liquor range is the published 14–20%"


def test_the_owners_category_choices_are_the_ones_the_server_accepts():
    """W7/W8 hand-off (10/15/26): the server refuses "other" from an owner, so the
    page offers owner_categories, and a product someone else sorted says who."""
    src = open(DASHBOARD, encoding="utf-8").read()
    body = src[src.index("function catControl(p)"):src.index("function catControl(p)") + 900]
    assert "DATA.owner_categories" in body
    assert "set_by_name" in src[src.index("set by you") - 200:src.index("set by you") + 200]


def test_the_range_bands_are_ember_never_green():
    """Owner, 10/15/26: "change these green backgrounds on the kpis to our
    orange color, green looks weird" — the meter band, the chart bands, the
    legend swatch, the counted chip and glow all read --br-range (ember)."""
    src = open(DASHBOARD, encoding="utf-8").read()
    a = src.index("/* ── Bar (owner, 10/14/26; redesigned")
    blk = src[a:src.index("function catControl", a)]
    assert "#bar{--br-range:var(--ember);" in blk
    for sel in (".br-meter .band{", ".br-meter.ctx .band{", ".br-mleg .bnd{", ".br-mleg .bnd.ok{", ".br-bchip.counted{"):
        rule = blk[blk.index(sel):blk.index("}", blk.index(sel))]
        assert "--br-range" in rule and "--hb-good" not in rule and "--green" not in rule, sel
    assert "bandCss: counted ?" not in blk and "opts.bandCss || 'var(--br-range)'" in blk


def test_every_unconfirmed_recipe_card_can_pick_its_product_and_names_it():
    # 10/15/26: a card with no matching product had no Confirm (Girls
    # Deserve's gin, the dirty martini), and a product the page's list didn't
    # carry read as its key ("euclid beverage north aurora|3101756").
    import bar.variance as bv
    row = {"id": 1, "provider": "rpower", "item_name": "Girls Deserve", "item_id": "9", "ingredient": "Gin",
           "product_key": "euclid|3101756", "oz": 1, "status": "suggested", "recipe": None, "note": None}
    assert bv._spec_public(row, {"euclid|3101756": "Tanqueray Gin 1L"})["product"] == "Tanqueray Gin 1L"
    assert bv._spec_public(dict(row, product_key=None), {})["product"] is None
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    body = src[src.index("  function specRow(s) {"):src.index("  function prodName(key)")]
    assert 'data-br-specp="' in body and 'data-act="confirm">Confirm' in body and "Pick the product" in body
    handler = src[src.index("a('data-br-spec') != null"):src.index("a('data-br-varall')")]
    assert "sb.product_key = spk" in handler and "Pick the product it pours first." in handler


def test_a_brand_word_beats_a_spirit_word_and_apostrophes_match():
    # 10/15/26: "WhistlePig Bourbon" and "Angel's Envy Bourbon" both matched
    # Barmen 1873 Bourbon on the word "bourbon"; "Angel's" never met "Angels".
    import bar.variance as bv
    prods = {"barmen": ("Barmen 1873 Bourbon", bv._words("Barmen 1873 Bourbon 6/750ML")),
             "ae": ("Angels Envy BBN", bv._words("Angels Envy BBN 86.6 750ML")),
             "wp": ("Whistlepig BBN Piggyback", bv._words("Whistlepig BBN Piggyback 6YR 100 750ML"))}
    assert bv._best_product("Angel's Envy Bourbon", prods)[0] == "ae"
    assert bv._best_product("WhistlePig Bourbon", prods)[0] == "wp"
    assert bv._best_product("Maker's Mark Bourbon", prods)[0] is None       # a brand nobody bought: none, never Barmen
    assert bv._best_product("Bourbon", prods)[0] == "barmen"               # no brand on the card: the kind may match
    recipes = json.load(open(os.path.join(ROOT, "docs", "clients", "simple-ejs", "bar_recipes.json"), encoding="utf-8"))
    walnut = next(r for r in recipes["recipes"] if r.get("rpower_item") == "Walnut Old Fash")
    assert "2 oz of Angel's Envy each" in walnut["rpower_also"][0]["note"]


def test_an_unmatched_drink_comes_with_a_checked_suggestion():
    # 10/15/26 ("the easiest way to correlate these so I don't mess up"): the
    # row arrives pre-filled — the product its name points at, a keg for a
    # draft size, the ounces its name states — and is only saved on Match.
    import bar.variance as bv
    lines = [{"kind": "item", "category": "beer", "description": "LITE 1/2 BBL", "uom": "KE", "product_key": "keg"},
             {"kind": "item", "category": "beer", "description": "LITE 24NR LSE", "uom": "CA", "product_key": "cans"},
             {"kind": "item", "category": "beer", "description": "AB 2 PRODUCT DISCOUNT", "uom": "EA", "product_key": "d"},
             {"kind": "item", "category": "liquor", "description": "TITOS VDK HANDMADE 1L", "uom": "CA", "product_key": "t"}]
    c = bv.pour_candidates(lines)
    assert "d" not in c                                                    # a discount is never a pour
    assert bv.suggest_pour("Miller Lite 22oz", "beer", c) == {"product_key": "keg", "product": "Lite 1/2 BBL", "oz": 22.0}
    assert bv.suggest_pour("Miller Lite", "beer", c)["product_key"] == "cans"   # no draft size: the cans
    assert bv.suggest_pour("Titos", "liquor", c) == {"product_key": "t", "product": "Titos VDK Handmade 1L", "oz": None}
    assert bv.suggest_pour("Blue Moon 22oz", "beer", c) == {}
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    row = src[src.index("  function varUnmapped(u) {"):src.index("  function specRow(s) {")]
    assert "sg && sg.product_key" in row and "check it, then Match" in row
