"""Owner review 10/17/26 (Erik's account, screenshots): the Bar's bought vs
sold by product, plate-cost columns, tooltips, Marketing and the Studio's
setup card."""
import os

from bar import variance as bv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    with open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def _line(key, desc, d, qty=1.0, cat="beer", uom="KE"):
    return {"product_key": key, "description": desc, "invoice_date": d, "qty": qty, "kind": "item",
            "category": cat, "uom": uom, "distributor": key.split("|")[0]}


# ── a distributor's new item code for the same product ───────────────────

def test_a_new_code_for_a_product_a_drink_pours_from_is_its_successor():
    old, new = "euclid|61542152", "euclid|5879152"
    lines = [_line(old, "N WHSTL GUMMYPALOOZA 1/2 BBL", "2026-06-24"),
             _line(new, "N WHSTL DA GUMMY 1/2 BBL", "2026-08-26"),
             _line(new, "N WHSTL DA GUMMY 1/2 BBL", "2026-10-14")]
    assert bv.successor_keys(lines, {old}, "2026-08-21", "2026-10-15") == {old: new}
    # Still bought in the span: no successor.
    assert bv.successor_keys(lines + [_line(old, "N WHSTL GUMMYPALOOZA 1/2 BBL", "2026-09-01")], {old},
                             "2026-08-21", "2026-10-15") == {}
    # The new code already pours a drink of its own: the owner tells them apart.
    assert bv.successor_keys(lines, {old, new}, "2026-08-21", "2026-10-15") == {}


def test_a_seasonal_rotation_or_another_size_never_joins():
    old = "euclid|1"
    rot = [_line(old, "SAM ADAMS SUMMER ALE 1/2 BBL", "2026-07-01"),
           _line("euclid|2", "SAM ADAMS OCTOBERFEST 1/2 BBL", "2026-08-25")]
    assert bv.successor_keys(rot, {old}, "2026-08-21", "2026-10-15") == {}
    size = [_line(old, "N WHSTL GUMMYPALOOZA 1/2 BBL", "2026-06-24"),
            _line("euclid|3", "N WHSTL GUMMYPALOOZA 1/6 BBL", "2026-08-26")]
    assert bv.successor_keys(size, {old}, "2026-08-21", "2026-10-15") == {}
    other_house = [_line(old, "N WHSTL GUMMYPALOOZA 1/2 BBL", "2026-06-24"),
                   _line("lakeshore|9", "N WHSTL GUMMYPALOOZA 1/2 BBL", "2026-08-26")]
    assert bv.successor_keys(other_house, {old}, "2026-08-21", "2026-10-15") == {}


def test_two_candidates_join_neither():
    old = "bt|1"
    lines = [_line(old, "HENDRICKS GIN 6B 1L", "2026-06-23", cat="liquor", uom="CA"),
             _line("bt|2", "HENDRICKS GIN ANOTHER 6B 1L", "2026-09-22", cat="liquor", uom="CA"),
             _line("bt|3", "HENDRICKS GIN NEUTRAL 6B 1L", "2026-09-29", cat="liquor", uom="CA")]
    assert bv.successor_keys(lines, {old}, "2026-08-21", "2026-10-15") == {}


def test_a_product_not_bought_in_the_span_reads_no_gap():
    src = open(os.path.join(ROOT, "bar", "variance.py"), encoding="utf-8").read()
    assert "not_bought = bool(bought == 0 and not p.units_unknown and last_bought)" in src
    page = _src()
    assert page.count("poured from stock bought before this span") == 2
    assert "'Last bought ' + hn(x.last_bought_mdy)" in page


def test_the_same_product_under_two_codes_at_once_joins():
    old, new = "euclid|57200", "euclid|57215"
    lines = [_line(old, "LITE 24NR HG", "2026-07-29", uom="CA"), _line(new, "LITE 24NR LSE", "2026-05-06", uom="CA"),
             _line(new, "LITE 24NR LSE", "2026-10-14", uom="CA")]
    assert bv.successor_keys(lines, {old}, "2026-08-21", "2026-10-15") == {old: new}


# ── Marketing: a brief whose lines are all answered has no bare heading ──

def test_a_brief_with_every_line_answered_has_no_recommendations_heading():
    import client_api
    text = "Erik, this week's biggest opportunity: Fill Tuesday.\n\n1. Post a thank-you.\n\n2. Run a Tuesday push."
    done = [{"index": 0, "answered": True}, {"index": 1, "answered": True}]
    html = client_api.format_insight_html(text, done)
    assert "Recommendations" not in html and "Fill Tuesday" in html
    html = client_api.format_insight_html(text, [{"index": 0, "answered": True}])
    assert "Recommendations" in html and "Run a Tuesday push" in html and "Post a thank-you" not in html


def test_marketing_has_no_how_you_compare_and_its_cards_carry_no_day_count():
    page = _src()
    assert 'data-bm-module="marketing"' not in page
    card = page[page.index("function mktOppCard(o, i) {"):page.index("function mktOppsSourceWords()")]
    assert "mktOppWhen(" not in card and 'class="when"' not in card
    for p in ("Features/Marketing/MarketingAnalyticsSection.swift", "Features/Labor/LaborAnalyticsSection.swift"):
        with open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", p), encoding="utf-8") as f:
            assert "HowYouCompareCard(" not in f.read(), p


# ── Plate costs: add-ons apart from dishes, linked to the POS by name ────

def test_add_on_buttons_link_to_one_recipe_and_never_to_a_sauce_of_them(db_path):
    import backoffice_recipes as br
    from models import Restaurant, create_restaurant, get_conn
    rid = create_restaurant(Restaurant(name="Addon Co", owner_email="a@x.test"), db_path=db_path)
    c = get_conn(db_path)
    for i, n in enumerate(["Add Grill Chix", "Classic Buffalo`$1.99", "Add Jalapeno", "BBQ`$1.99", "Well Done",
                           "Xtra Ranch"]):
        c.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_name, item_kind, "
                  "kind, qty, sales, sales_category) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (rid, "rpower", f"l{i}", "2026-10-10", n, "modifier", "sale", 1, 2.0,
                   "(Modifiers)" if n == "Well Done" else "(Food Add-Ons)"))
    c.commit(); c.close()
    bk = {"recipes": [{"name": f"MOD- {n}", "group": "MENU ITEM>MODS", "ingredients": []} for n in
                      ("ADD GRILLED CHICKEN", "WING SAUCE CLASSIC BUFFALO", "JALAPENO RANCH",
                       "WING SAUCE DR. PEPPER BBQ", "WING SAUCE BBQ CHIP SPICE RUB", "RANCH DRESSING")],
          "items": [], "pos_names": {}}
    links = br.addon_links(rid, bk, "2026-10-01", "2026-10-15", db_path)
    assert links["MOD- ADD GRILLED CHICKEN"] == ["Add Grill Chix"]
    assert links["MOD- WING SAUCE CLASSIC BUFFALO"] == ["Classic Buffalo`$1.99"]
    assert "MOD- JALAPENO RANCH" not in links, "the peppers are not the ranch, nor the plain ranch"
    assert links["MOD- RANCH DRESSING"] == ["Xtra Ranch"]
    assert links["MOD- WING SAUCE DR. PEPPER BBQ"] == ["BBQ`$1.99"], "the closest fit wins"
    assert not any("Well Done" in v for v in links.values())
    assert br._is_addon({"group": "MENU ITEM>MODS", "name": "MOD- ADD EGG"}) and not br._is_addon(
        {"group": "MENU ITEM>PLATES", "name": "SMASHBURGER"})
    assert br.addon_name("MOD- ADD GRILLED CHICKEN") == "ADD GRILLED CHICKEN"


# ── tips: short, in points, the app's text face ──────────────────────────

def test_long_tips_open_as_points_in_the_text_face():
    page = _src()
    assert "function cavTipList(lines,lead)" in page and "function cavTipFormat(p)" in page
    assert "if(on){cavTipFormat(p);" in page, "every tip is formatted as it opens"
    assert ".cav-it .cav-itp,.cav-it .cav-itp *:not(.hb-num),.cav-itp.cav-itp-fixed,.cav-itp.cav-itp-fixed *:not(.hb-num){font-family:'Apfel Grotezk',sans-serif!important}" in page


def test_the_tip_splitter_keeps_abbreviations_and_elements_whole():
    import subprocess
    page = _src()
    js = page[page.index("var CAV_TIP_ABBR"):page.index("function cavTipFormat")]
    out = subprocess.run(["node", "-"], input=js + "console.log(JSON.stringify(["
                         "cavTipSplit('Brunch in St. Charles is busy. Totals add <span>1.5</span> nights. Done.'),"
                         "cavTipSplit('<b>One. Two</b> stays whole. Next one.')]))", capture_output=True, text=True)
    a, b = __import__("json").loads(out.stdout)
    assert a == ["Brunch in St. Charles is busy.", "Totals add <span>1.5</span> nights.", "Done."]
    assert b == ["<b>One. Two</b> stays whole.", "Next one."]


# ── the Bar: Good to know is green; What this measures is a dialog ───────

def test_good_to_know_is_green_and_what_this_measures_is_a_dialog():
    page = _src()
    ins = page[page.index("  function insights2(list) {"):page.index("  function overview(d, pc) {")]
    assert "info: 'var(--hb-good)'" in ins and "info: 'green'" in ins
    assert '<template id="br-why-tpl">' in page and "function whyOpen()" in page
    assert "root.appendChild(m);" in page and "cModal.open('br-why-modal', {close: 'barWhyClose'})" in page


# ── the Studio's setup card ──────────────────────────────────────────────

def test_the_studio_setup_tabs_are_one_rounded_strip_with_no_sticky_bars():
    page = _src()
    assert "#ss-settings-setup #ss-settings .ss-tabs{position:static;display:flex" in page
    assert "border-radius:14px;background:var(--sf-recess)" in page
    assert "#ss-settings-setup .sw-gen{position:static;background:none}" in page
    assert ".ss-stage[data-stage=\"setup\"] .ss-hero{text-align:center;margin:0 0 22px}" in page


# ── 10/17/26 afternoon: tips above everything; long lists fold ────────────

def test_an_open_tip_is_lifted_to_a_fixed_opaque_layer():
    page = _src()
    assert "function cavTipLift(p,b)" in page and "document.body.appendChild(p);p.classList.add('cav-itp-fixed')" in page
    assert ".cav-itp.cav-itp-fixed{position:fixed;z-index:10050;" in page and "background:var(--hp-act-bg);opacity:1}" in page
    assert "if(on){cavTipFormat(p);p.hidden=false;cavTipLift(p,b);}else{p.hidden=true;cavTipHome(p);}" in page


def test_guest_mentions_fold_after_two_and_each_quote_is_one_line():
    page = _src()
    assert "var MENTION_SHOW = 2" in page and "mem-ment-more" in page and "_pq.moreOpen" in page
    assert "#lb2-wait-people .mem-quote{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" in page


def test_dishes_and_add_ons_each_show_their_first_few():
    page = _src()
    assert "var FC2_PL_SHOW=8,FC2_AO_SHOW=5;" in page
    assert "fc2-pl-more fc2-ao-more" in page and "All '+ao.length+' add-ons" in page


# ── the Bar: green good news, the app's glow charts ──────────────────────

def test_good_news_on_the_bar_is_green_and_tiles_keep_their_tone():
    page = _src()
    assert "#bar .cbtn.br-sig.t-good{--c:var(--green)}" in page, "the tone beats #bar .cbtn.br-sig's ember"
    assert "[data-theme=\"dark\"] #bar .br-feat.br-good{" in page and "corner-tl-green-v1.png" in page
    assert os.path.exists(os.path.join(ROOT, "static", "img", "glow", "corner-tl-green-v1.png"))


def test_the_bars_hero_and_category_lines_use_the_app_glow_chart():
    page = _src()
    assert "function glowChart(vals, key, labels, opts)" in page and "C.glowLine(" in page
    assert "glowChart(spark, 'total', labels, {cls: 'br-glow-hero'" in page
    assert "glowChart(sp, c.key, sparkLabels(pc), {cls: 'br-glow-spk'" in page and "floor: fit.min, ceil: fit.max" in page


def test_a_tip_closes_only_when_its_own_i_scrolls():
    page = _src()
    assert "(h&&t&&t.contains&&t.contains(h))" in page
