"""A third round of Labor-page polish, from a fresh screenshot pass:

- The Employee Availability heading sat flush against the Generate/Update
  buttons above it (0px gap, measured live) — no margin-top existed to
  create one, unlike the 16px gap already used between the Schedule
  heading and those same buttons.
- The donut chart's legend already showed the full role name on hover
  (via `title`) once the visible label got truncated with "…" — but the
  cursor never changed, so nothing suggested a role was hoverable.
- The week radar's own headline number ("27.2%") is a plain `.hb-num`
  with no `.stat-n` (it needs its own countUp trigger, not
  animateStatNums' generic one), so it inherited `.lb2-sg .v`'s 32px/600
  instead of matching its two siblings ($19k total, the vs-industry %) at
  `.stat-n`'s 26px/700.
- The Overstaffed days card and the Overtime alerts card both used the
  same orange border (`rgba(200,75,47,...)`, the brand ember) and the
  Overtime card's own "at risk" hour figure and badge reused that same
  orange (#ff8a65) rather than a red — the two dark cards read as the
  same color despite meaning different things.
- Every chart/number reveal (donut sweep, week radar draw, the shared
  glowLine/bars/stacked chart family used by Home/Reviews/Labor/Intel,
  and animateStatNums'/animateBars' count-ups and fills) fired the
  instant its data loaded, regardless of scroll position — invisible if
  the chart was any distance below the fold, since the animation was
  long over before a scroll ever reached it.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _fn(name):
    s = _src()
    m = re.search(r"function %s\(" % re.escape(name), s) or \
        re.search(r"window\.%s\s*=\s*function\(" % re.escape(name), s)
    assert m, "function not found: " + name
    i = s.index("{", m.start())
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[m.start():j + 1]
    raise AssertionError("unbalanced braces in " + name)


# ── Employee Availability / Operational Score spacing ───────────────────────

def test_the_three_schedule_rows_share_one_wrapper_and_one_gap():
    """These used to be three bare divs with hand-typed 16px margins that
    drifted (14px on one). They are now .lb2-srow tiers inside the ember
    .lb2-sched container, so the gap comes from one rule and the whole row
    is the click target."""
    s = _src()
    for marker, cls, fn in (("<!-- Employee Availability Manager -->", "s1", "toggleAvailPanel"),
                            ('Operational Score <span id="team-coverage-chip"', "s2", "toggleTeamPanel"),
                            ('<div class="lb2-subsection-title">Daily Tasks</div>', "s3", "toggleTasksPanel")):
        i = s.index(marker)
        around = s[max(0, i - 300):i + 300]
        assert f'<div class="lb2-srow {cls}">' in around, marker
        assert f'onclick="lb2RowClick(event,{fn})"' in around, marker
        assert 'style="margin-bottom:16px"' not in around and 'margin-top:16px;margin-bottom:16px' not in around, marker
    m = re.search(r"\.lb2-srow\{([^}]*)\}", s)
    assert m and "margin-top:12px" in m.group(1)
    assert ".lb2-srow.s2{" in s and ".lb2-srow.s3{" in s, "each row is its own shade"
    assert "function lb2RowClick(e, fn)" in s
    # no dash bar on these titles any more — only section headers keep it
    assert ".lb2-subsection-title:before" not in s
    assert ".hb-sh h2:before{" in s


# ── donut legend hover affordance ────────────────────────────────────────────

def test_donut_legend_role_name_shows_a_pointer_cursor():
    body = _fn("renderRoleDonut")
    m = re.search(r'<span title="\'\+_escHtml\(d\.name\)\+\'"[^"]*style="([^"]*)"', body)
    assert m, "couldn't find the role-name legend span"
    assert "cursor:pointer" in m.group(1)


# ── week radar headline number matches its siblings ──────────────────────────

def test_week_radar_number_matches_its_sibling_stat_sizes():
    css = _src()
    m = re.search(r"\.lb2-sg \.v #lb2-radar-n\{([^}]*)\}", css)
    assert m, "missing a size override for #lb2-radar-n"
    assert "font-size:26px" in m.group(1) and "font-weight:700" in m.group(1)


# ── overtime alerts read as red, not the overstaffed orange ─────────────────

def test_overtime_card_border_is_not_the_overstaffed_orange():
    s = _src()
    i = s.index('>Overtime alerts<')  # the heading markup, not the CSS comment above it
    card = s[i:i + 400]
    m = re.search(r'class="dark-hero-card[^"]*" style="border:1px solid (rgba\([\d,.]+\))', card)
    assert m, "couldn't find the overtime alerts card border"
    assert m.group(1) != "rgba(200,75,47,.45)", \
        "overtime alerts should not share the overstaffed card's orange border"
    assert "ot-hero-card" in card, "overtime card should carry its own background-override class"


def test_overtime_at_risk_hour_figure_and_badge_are_red_not_orange():
    s = _src()
    i = s.index(">Overtime alerts<")  # the heading markup, not the CSS comment above it
    j = s.index("{% endfor %}", i)
    block = s[i:j]
    assert "#ff8a65" not in block, "overtime block still uses the overstaffed orange somewhere"
    assert block.count("#ff5a5a") >= 2, "expected the at-risk hour figure and the review-pay badge in red"


# ── scroll-triggered chart/number reveals ────────────────────────────────────

def test_cavnar_when_visible_helper_exists():
    s = _src()
    assert "function cavnarWhenVisible(el,fn)" in s
    assert "IntersectionObserver" in s[s.index("function cavnarWhenVisible"):s.index("function cavnarWhenVisible") + 400]


def test_chart_draw_primitives_are_paused_until_in_view():
    css = _src()
    assert ".hb-draw,.hb-fill,.hb-glow,.hb-dot,.hb-hot,.hb-bar{animation-play-state:paused}" in css
    assert ".cm-inview .hb-draw" in css and "animation-play-state:running" in css


def test_cm_chart_reveal_is_wired_into_the_shared_motion_mutation_observer():
    body = _fn("cavnarMotionInit")
    assert "_cmChartReveal(root)" in body


def test_donut_sweep_and_total_wait_for_visibility():
    body = _fn("renderRoleDonut")
    assert body.count("cavnarWhenVisible(svgEl") >= 2, \
        "both the segment sweep and the $ total tween should wait for scroll"


def test_week_radar_countup_waits_for_visibility():
    body = _fn("renderWeekRadar")
    assert "cavnarWhenVisible(el,function(){" in body.replace(" ", "")


def test_animate_stat_nums_waits_for_visibility_per_element():
    body = _fn("animateStatNums")
    assert "cavnarWhenVisible(el, function ()" in body or "cavnarWhenVisible(el, function()" in body


def test_animate_bars_waits_for_visibility_per_bar():
    body = _fn("animateBars")
    assert "cavnarWhenVisible(bar, function ()" in body or "cavnarWhenVisible(bar, function()" in body
