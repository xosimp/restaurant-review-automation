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
