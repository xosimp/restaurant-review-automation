"""Two rounds of visual debt in the Labor module, both fixed source-side.

Round one: a redundant orange kicker, a cramped period-then-badge header, a
stale "your labor is at X%" paragraph nothing else on the page still fed,
small text a shade cooler than the sand tone the iOS app already uses,
sales and labor numbers buried in a caption-sized row, and a "Manage"
button that toggled a panel far down the page while doing nothing visible
where it sat.

Round two, after a closer look at round one's own result: the margin-left
used to add space after the period indented the status badge whenever it
wrapped to its own line (which it almost always did — the stat chips were
taking the header row's width first); the date range and its target
fragment then also wrapped, because that same squeeze left the title
column ~310px wide; the schedule buttons sat in the header instead of by
the feature they trigger; the labor figure had no percent-of-sales next to
it; the consultant tag was missing "AI"; and everything below it read
smaller than everywhere else the same pass had already enlarged.

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
    """word-spacing, not a margin on the badge itself — a margin-left
    survives a line wrap and offsets the badge from the left edge whenever
    the header doesn't fit on one line (it routinely doesn't; the stat
    chips claim most of the row's width first). A trailing space is
    trimmed to nothing at a wrap point, so word-spacing widens the gap
    only when both pieces share a line, and never indents the second."""
    s = _src()
    assert re.search(r"\.hb-h1\{[^}]*word-spacing", s), \
        "no spacing rule on the shared module-header title"
    assert not re.search(r"\.hb-h1 \.hb-head\{[^}]*margin-left", s), \
        "a margin-left on the badge re-introduces the wrap-indent bug"


def test_the_title_column_is_no_longer_squeezed_by_the_stat_chips():
    """The chips (labor/target/overstaffed/overtime/gap) took their full
    natural width first and left whatever was left — on a normal desktop
    that was ~310px, not enough for the headline, the date range, or the
    two big numbers without wrapping mid-phrase. This has to be an inline
    style: the shared .hb-top markup sets `style="min-width:0;flex:1"` on
    that column inline, which beats any external stylesheet rule of equal
    or lower specificity, so a CSS-only fix here would be silently
    overridden and never take effect."""
    panel = _labor_panel()
    m = re.search(r'<div class="hb-top">\s*<div style="([^"]*)">', panel)
    assert m, "couldn't find the labor header's title column"
    style = m.group(1)
    assert re.search(r"flex:\s*1\s+1\s+\d{3,}px", style), (
        "the title column needs a real flex-basis reserved, or the stat "
        "chips crowd it back down to a few hundred pixels: " + style
    )


def test_the_schedule_and_upload_buttons_moved_off_the_header():
    """They did nothing where they sat — sales and labor numbers, not
    scheduling controls, belong in the header's first glance. Both now
    sit beside the feature they actually act on."""
    panel = _labor_panel()
    header_end = panel.index('"lb2-bignums"')
    assert 'class="lb2-actions"' not in panel[:header_end], \
        "the action buttons are still up in the header"
    sched_heading = panel.index("Next week, built by Cavnar")
    actions_idx = panel.index('class="lb2-actions"', header_end)
    assert sched_heading < actions_idx < panel.index("sched-tbody"), (
        "Generate schedule / Upload shifts CSV should sit with the "
        "Schedule section, not floating somewhere else on the page"
    )


def test_the_date_range_no_longer_repeats_the_target_next_to_it():
    """The target already has its own stat chip a few inches to the
    right ("26% target") — restating it in the date-range line too was
    redundant. The chip stays; only the inline repeat is gone."""
    panel = _labor_panel()
    m = re.search(r'<div class="lb2-daterange">(.*?)</div>', panel)
    assert m, "couldn't find the date-range row"
    assert "target" not in m.group(1).lower()
    assert 'id="labor-period"' in m.group(1)
    # the stat chip still carries it, further along in the header
    assert re.search(r'<span class="l">target</span>', panel)


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


def test_the_labor_number_carries_its_own_pct_colored_against_target():
    """The dollar figure alone doesn't say whether it's good — the percent
    of sales it represents does, and it needs to read green or red at a
    glance rather than making the owner do the division themselves."""
    panel = _labor_panel()
    m = re.search(
        r'<span class="l">Labor</span>.*?class="hb-num pct '
        r"\{\{ 'good' if _lp <= _lt else 'bad' \}\}\">\{\{ _lp \}\}%</span>",
        panel,
    )
    assert m, "no colored labor % next to the labor dollar figure"
    css = _src()
    assert ".lb2-bn .pct.good{color:var(--hb-good)}" in css
    assert ".lb2-bn .pct.bad{color:var(--hb-bad)}" in css


def test_sales_and_labor_stack_with_a_tight_gap():
    """These read as two rows, sales above labor — not a wrapping accident
    from a squeezed container, which is what produced the oversized gap
    between them in the first place."""
    css = _src()
    m = re.search(r"\.lb2-bignums\{([^}]*)\}", css)
    assert m and "flex-direction:column" in m.group(1)
    gap = re.search(r"gap:(\d+)px", m.group(1))
    assert gap and int(gap.group(1)) <= 12, \
        "the gap between sales and labor should read as one group, not two"


def test_cavnar_ai_gets_its_name_in_the_labor_consultant_box():
    panel = _labor_panel()
    assert "Cavnar AI's read on your labor" in panel
    assert "Cavnar's read on your labor" not in panel


def test_labor_body_text_below_the_consultant_box_was_actually_enlarged():
    """Spot-checks a few of the classes that render below the "Cavnar AI's
    read" box — the AI insight paragraph, a stat-tile value, and a money
    figure — against their sizes before this pass, so a future edit that
    quietly shrinks one of them again gets caught."""
    css = _src()

    def size_of(selector):
        m = re.search(re.escape(selector) + r"\{([^}]*)\}", css)
        assert m, "missing rule: " + selector
        sz = re.search(r"font-size:([\d.]+)px", m.group(1))
        assert sz, "no font-size on: " + selector
        return float(sz.group(1))

    assert size_of(".lb2-ai") >= 16
    assert size_of("#panel-labor #labor-insight") >= 16
    assert size_of(".lb2-sg .v") >= 32
    assert size_of(".lb2-money .x") >= 17
    assert size_of(".lb2-events li") >= 15
    assert size_of(".lb2-hist .row") >= 15


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
