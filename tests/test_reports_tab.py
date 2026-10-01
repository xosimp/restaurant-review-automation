"""Reports, one tap from anywhere (owner, 9/30/26): "we need to have a spot
called Reports where owners can easily view all reports instead of always
having to go to the home page to click on Report."

A top-level Reports tab beside Home opens the daily report panel on its
list - every night, newest first, with the 3-second status the list API
already carries (dsr.access.summary) - and Night, Week and Period sit
beside it. #dsr/list is the address."""
import os
import re

SRC = open(os.path.join(os.path.dirname(__file__), "..", "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_reports_tab_sits_beside_home_and_opens_the_list():
    tabs = SRC[SRC.index('<div class="tabs" role="tablist"'):SRC.index('<span id="tab-indicator">')]
    assert tabs.index('id="tab-home"') < tabs.index('id="tab-reports"') < tabs.index('id="tab-reviews"')
    assert "dsrOpen('list')" in tabs
    assert "var home=el('tab-reports')||el('tab-home');" in SRC        # the panel lights its own tab


def test_the_list_is_every_night_newest_first_with_older_on_request():
    assert 'data-kind="list" aria-pressed="false">All reports</button>' in SRC
    assert "if(kind==='list')return loadList(null);" in SRC
    body = SRC[SRC.index("function loadList(before){"):SRC.index("document.addEventListener('click',function(e){\n    var t=e.target&&e.target.closest?e.target.closest('[data-dr-older]')")]
    assert "get('/api/dsr?limit=30'" in body and "data-dr-older=" in body
    row = SRC[SRC.index("function listRow(r){"):SRC.index("function loadList(before){")]
    assert 'data-dr="open" data-kind="night"' in row and "r.verdict" in row and "r.net" in row
    assert re.search(r"\(week\|period\|list\)", SRC)                   # #dsr/list routes


# ── Owner, 9/30/26: the list reads as figures, the tab bar and pills ────────

def test_the_reports_page_has_no_home_link_and_its_hover_fills_the_row():
    nav = SRC[SRC.index('<div class="dr-nav">'):SRC.index('<div id="dr-body"')]
    assert 'data-dr="home"' not in nav and "&larr; Home" not in nav
    assert ".dr-list{padding:0!important}" in SRC


def test_a_hovered_row_is_an_opaque_lifted_tile_past_the_card_edge():
    # Owner, 9/30/26: the translucent tint stopped at the card's 1px border.
    # The hovered row is now its own surface, lifted and scaled, so the list
    # card must not clip it and reduced motion keeps it still.
    assert "overflow:hidden}" not in SRC[SRC.index(".dr-list{padding:0"):][:40]
    hov = SRC[SRC.index("#panel-dsr .dr-list-row:hover,#panel-dsr .dr-list-row:focus-visible{"):]
    hov = hov[:hov.index("}")]
    for part in ("background-color:var(--surface)", "box-shadow:var(--dr-lift)",
                 "scale(1.025)", "border-radius:14px"):
        assert part in hov, part
    assert "#panel-dsr .dr-list-row:hover+.dr-list-row" in SRC
    assert ("@media (prefers-reduced-motion:reduce){ #panel-dsr .dr-list-row,"
            "#panel-dsr .dr-list-row:hover") in SRC


def test_each_row_shows_figures_not_the_paragraph():
    fn = SRC[SRC.index("function listRow("):SRC.index("function loadList(")]
    assert "r.lead" not in fn and "listStats(r.stats)" in fn
    assert ".dr-list-l{" not in SRC


def test_the_figures_are_only_what_the_night_measured():
    from dsr import access
    import dsr
    facts = {"blocks": {
        "sales": {"status": dsr.READY, "metrics": {"vs_last_week_pct": 106.1, "vs_forecast_pct": -7.9,
                                                    "guests": 312, "avg_ticket": 35.36}},
        "labor": {"status": dsr.READY, "metrics": {"pct": 25.5, "target_pct": 35.0, "overtime_hours": 0}}}}
    got = {x["key"]: (x["value"], x["tone"]) for x in access.list_stats(facts)}
    assert got == {"vs_last_week_pct": ("+106%", "good"), "vs_forecast_pct": ("-8%", "bad"),
                   "labor_pct": ("25.5%", "good"), "guests": ("312", None), "avg_ticket": ("$35.36", None)}
    # a block that isn't ready says nothing, and no figure is invented as 0
    facts["blocks"]["labor"]["status"] = "waiting"
    facts["blocks"]["sales"]["metrics"] = {}
    assert access.list_stats(facts) == []


def test_home_and_reports_are_set_apart_from_the_modules():
    tabs = SRC[SRC.index('<div class="tabs" role="tablist"'):SRC.index('<span id="tab-indicator">')]
    assert tabs.index('id="tab-reports"') < tabs.index('class="tab-sep"') < tabs.index('id="tab-reviews"')
    assert ".tabs .tab-sep{" in SRC


def test_the_module_pills_catch_light_on_their_top_edge():
    rule = SRC[SRC.index(".hb-chip{display:inline-flex"):]
    rule = rule[:rule.index("}") + 1]
    assert "inset 0 1px 0 rgba(255,255,255," in rule and "linear-gradient(180deg,rgba(255,255,255,.12)" in rule
