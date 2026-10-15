"""The Bar page's redesign (owner, 10/14/26): the page script runs under node
against a payload bar_cost builds from real invoice lines, so what an owner
sees is checked, not the source's shape."""
import json
import os
import re
import shutil
import subprocess
from datetime import date

import pytest

import bar_cost as bc
import fintech_invoices as fi
import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")
HEAD = ("Retailer Name,Vendor Name,Location ID,Invoice Number,Invoice Date,Invoice Total,Product Number,"
        "Product Description,U.P.C,Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n")


def _row(vendor, inv, day, code, desc, uom, qty, cost):
    return f'EJs,"{vendor}",1,{inv},{day},${qty * cost:.2f},{code},{desc},0,{uom},{qty},${cost:.2f},$0.00\n'


def _bar_script():
    src = open(DASHBOARD, encoding="utf-8").read()
    start = src.index("  var OPEN = false, BACK_TO = null, FROM_DASH = false, DATA = null, GID = 0;")
    end = src.index("})();\n</script>", start)
    return "(function () {\n" + src[start:end] + "})();\n"


HARNESS = r"""
var store={};
function el(){return {innerHTML:'',textContent:'',style:{},hidden:true,classList:{add:function(){},remove:function(){},toggle:function(){}},
  setAttribute:function(){},getAttribute:function(){return null;},addEventListener:function(){},appendChild:function(){},
  querySelector:function(){return null;},querySelectorAll:function(){return [];},focus:function(){}};}
var document={hidden:false,body:el(),getElementById:function(){return null;},createElement:function(){return el();},
  querySelector:function(){return null;},querySelectorAll:function(){return [];},addEventListener:function(){}};
var window={location:{pathname:'/'},history:{pushState:function(){},replaceState:function(){},back:function(){}},
  matchMedia:function(){return {matches:true};}};
var history=window.history;
"""


def _render(payload):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = HARNESS + _bar_script() + "console.log(window._barRender(%s));" % json.dumps(payload, default=str)
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture
def payload(db_path):
    rid = models.create_restaurant(models.Restaurant(name="EJ's", owner_email="o@x.test"), db_path=db_path)
    csv = HEAD
    # Ten products whose price went up and held for two orders (a bigger
    # change times more cases down the list; a change counts only once it
    # persists, V2), one that came down, and a tax line that is no product.
    for k in range(10):
        csv += _row("Breakthru Beverage", f"A{k}", "08/10/2026", f"{100 + k}", f"VODKA NUMBER {k} 1L", "CA", 1, 100.0)
        for inv, day in (("B", "09/10/2026"), ("D", "09/20/2026")):
            csv += _row("Breakthru Beverage", f"{inv}{k}", day, f"{100 + k}", f"VODKA NUMBER {k} 1L", "CA", 2 + k,
                        100.0 + 3 * (k + 1))
    csv += _row("Chicago Beverage", "C1", "08/12/2026", "900", "DAISY CUTTER K 1/2BB", "KE", 2, 229.0)
    csv += _row("Chicago Beverage", "C2", "09/12/2026", "900", "DAISY CUTTER K 1/2BB", "KE", 2, 219.0)
    csv += _row("Chicago Beverage", "C3", "09/19/2026", "900", "DAISY CUTTER K 1/2BB", "KE", 2, 219.0)
    csv += _row("Elgin Beverage", "E1", "08/12/2026", "99901", "SALES TAX", "EA", 1, 5.25)
    csv += _row("Elgin Beverage", "E2", "09/12/2026", "99901", "SALES TAX", "EA", 1, 1.58)
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db_path)
    conn = models.get_conn(db_path)
    for day in ("2026-09-08", "2026-09-10", "2026-09-12"):
        for cat, v in (("Beer", 2000), ("Liquor", 9000), ("Wine", 0)):
            conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                         "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, day, "sales.cat:" + cat, v))
    conn.commit()
    conn.close()
    return bc.build(rid, db_path=db_path, today=date(2026, 10, 1))


def test_the_page_leads_with_the_four_week_reading_and_its_range(payload):
    html = _render(payload)
    hero = html[html.index('class="br-card br-hero'):html.index('class="br-cats"')]
    assert "pour cost over the last four weeks" in hero
    assert "your mix’s range" in hero, "the range is this bar's own mix, said as such"
    assert 'class="br-meter"' in hero
    for label in ("Beer &amp; seltzer", "Liquor", "Wine"):
        assert label in html


def test_insights_lead_and_each_has_a_headline_a_figure_and_an_action(payload):
    html = _render(payload)
    assert "What deserves a look" in html
    cards = re.findall(r'class="br-card lift br-in br-rise".*?</article>', html, re.S)
    assert cards and len(cards) == len(payload["insights"])
    for c in cards:
        assert 'class="h"' in c and 'class="m"' in c and 'class="a"' in c


def test_price_watch_opens_on_the_top_increases_by_money_and_expands_to_all(payload):
    html = _render(payload)
    rows = re.findall(r'data-br-row="([^"]+)"', html)
    ups = payload["price_watch"]["increases"]
    assert len(ups) == 10 and len(rows) == 8, "the eight biggest increases first"
    assert rows[0].startswith(ups[0]["product_key"]) and ups[0]["impact_90d"] == max(r["impact_90d"] for r in ups)
    assert "Show all" in html and ">10</span> changes" in html
    assert "SALES TAX" not in html, "a tax line is never a price change"
    # The estimate is said as one, everywhere it shows.
    assert "at 90-day buying" in html and "not a bill already paid or a saving earned" in html
    assert "not money already paid" in html


def test_the_trend_the_monthly_table_distributors_and_sorting_are_all_still_there(payload):
    html = _render(payload)
    for piece in ("Pour cost, week by week", "Month by month, as a table", "Where the money goes",
                  "How every product is sorted", 'data-br-series="total"'):
        assert piece in html, piece


def test_every_button_is_a_system_button(payload):
    html = _render(payload)
    for tag in re.findall(r"<button[^>]*>", html):
        assert "cbtn" in tag, tag


def test_no_invoices_and_no_overlap_say_so(payload):
    assert "waiting on invoices" in _render({"ok": True, "available": False, "why_not": "No invoices."})
    assert "couldn’t be read" in _render(None)


def test_motion_is_off_under_reduce_motion_and_the_tokens_are_the_pages_own():
    src = open(DASHBOARD, encoding="utf-8").read()
    css = src[src.index("/* ── Bar (owner, 10/14/26; redesigned"):src.index("</style>", src.index("/* ── Bar (owner, 10/14/26; redesigned"))]
    assert "@media (prefers-reduced-motion:reduce)" in css
    # --hb-* are scoped to the panels: the Bar page declares its own, or every
    # tone colour and hairline on it is empty (they were, before 10/14/26).
    assert "#bar{--hb-line:" in css and "--hb-good:var(--green)" in css
    assert "stroke-dasharray" not in css, "a dash draw-in does not survive non-scaling strokes"
