"""Six things a walkthrough of the Labor module flagged as visual debt:
a redundant orange kicker, a cramped period-then-badge header, a stale
"your labor is at X%" paragraph nothing else on the page still fed, small
text a shade cooler than the sand tone the iOS app already uses, sales and
labor numbers buried in a caption-sized row, and a "Manage" button that
toggled a panel far down the page while doing nothing visible where it sat.

Asserted against the template source, same approach as
test_frontend_rules.py — there is no request that renders this panel's
markup back as a payload worth diffing; the markup itself is the surface.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _labor_panel():
    """The Labor module's own markup block, not the whole 11k-line file —
    a couple of these strings (the kicker pattern, the sub-header row)
    recur on every module, and a check against the whole file would pass
    by matching Reviews or Food Cost instead of Labor."""
    s = _src()
    start = s.index('id="panel-labor"')
    end = s.index('id="panel-food-cost"' if 'id="panel-food-cost"' in s[start:] else 'ACCOUNT', start)
    return s[start:end]


def test_the_orange_kicker_above_the_labor_headline_is_gone():
    panel = _labor_panel()
    assert 'hb-kicker">Labor ·' not in panel


def test_the_period_before_the_status_word_has_room_to_breathe():
    s = _src()
    assert re.search(r'\.hb-h1 \.hb-head\{[^}]*margin-left', s), \
        "no spacing rule on the shared module-header status badge"


def test_the_stale_gap_paragraph_is_gone():
    panel = _labor_panel()
    assert 'id="gap-dollar"' not in panel
    assert "at or below your" not in panel
    assert "working out the monthly gap" not in panel


def test_the_gap_dollar_js_updates_are_still_null_guarded():
    """The element is gone; the fetch handlers that used to write into it
    must still check before touching it, or a stray reference throws."""
    s = _src()
    assert "getElementById('gap-dollar')" in s, \
        "this test's premise (JS still looks it up) no longer holds"
    for m in re.finditer(r"\bmsgEl\.(?:textContent|style)", s):
        line_start = s.rfind("\n", 0, m.start()) + 1
        line = s[line_start:m.start()]
        assert re.search(r"if\s*\(\s*msgEl\s*\)", line), \
            "an unguarded write to the removed #gap-dollar element: " + line.strip()


def test_labor_small_text_matches_the_ios_sand_tone_in_dark_mode():
    s = _src()
    m = re.search(r'\[data-theme="dark"\]\{[^}]*--ink3:(#[0-9a-fA-F]{6})', s)
    assert m, "no dark-mode --ink3 token"
    assert m.group(1).lower() == "#cdbfa9", (
        "dark --ink3 should match iOS's Ink3 dark asset (#CDBFA9), not a "
        "cooler gray picked independently for the web"
    )


def test_sales_and_labor_are_rendered_as_the_big_numbers():
    panel = _labor_panel()
    assert '"lb2-bignums"' in panel
    assert re.search(r'class="lb2-bn"><span class="l">Sales</span>', panel)
    assert re.search(r'class="lb2-bn"><span class="l">Labor</span>', panel)
    m = re.search(r"\.lb2-bn \.hb-num\{([^}]*)\}", _src())
    assert m and int(re.search(r"font-size:(\d+)px", m.group(1)).group(1)) >= 28, \
        "sales/labor need to read as the headline figures, not a caption"


def test_the_date_range_row_sits_above_the_big_numbers():
    panel = _labor_panel()
    assert panel.index('"lb2-daterange"') < panel.index('"lb2-bignums"')
    # and it still carries the id the date-range JS fills in after load
    assert '<span id="labor-period"' in panel


def test_only_one_availability_toggle_button_remains():
    """There were two elements sharing id="avail-toggle-btn" — the one in
    the header actions row (dead weight; the panel it opened was out of
    view) and the one that actually sits beside Employee Availability.
    Only the second should remain, and duplicate ids should be gone."""
    panel = _labor_panel()
    assert panel.count('id="avail-toggle-btn"') == 1
    assert "Manage availability" not in panel
    idx = panel.index('id="avail-toggle-btn"')
    nearby = panel[max(0, idx - 300):idx]
    assert "Employee Availability" in nearby
