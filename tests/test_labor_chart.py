"""The Labor ribbon chart: two reported bugs and a decision-support pass
on top of the same chart, with the request to NOT rebuild it — the glow
line, its draw-in animation, and its layout stay exactly as they were.

Bug 1: the "8 weeks" pill carried a permanent dark-orange background
whether it was selected or not — one stray line, unconditional, set on
every page load regardless of which tab was active.

Bug 2: the area fill under the curve was one flat color everywhere, even
across the days already marked with the red over-target dots — the
notches and the fill disagreed about which days were bad.

On top of the fix: a real hover tooltip (day, %, sales, labor cost,
hours, all pulled from data already on the page — nothing invented), and
one data-grounded callout naming the worst day, which disappears
entirely on a healthy period rather than manufacturing something to say.

Asserted against the template source, same approach as
test_labor_module_ui.py — there is no response payload this chart's
markup renders into that's worth diffing against.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _fn(name):
    """One JS function's source, isolated by brace-matching from its
    `function name(...)` line to the matching close — these chart
    helpers are hundreds of lines away from each other and from any
    panel boundary, so a plain substring search would as easily match
    a neighboring function's body."""
    s = _src()
    start = s.index("function %s(" % name)
    i = s.index("{", start)
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[start:j + 1]
    raise AssertionError("unbalanced braces in " + name)


# ── Bug 1: the pill background ────────────────────────────────────────────

def test_the_trend_pill_no_longer_gets_a_forced_background_on_load():
    s = _src()
    assert "_perfActiveColor()" not in s or "_pt.style.background" not in s, (
        "a stray unconditional style write would repaint the pill regardless "
        "of whether it's actually selected"
    )
    assert "getElementById('perf-tab-trend')" not in s.split(
        "function switchPerfTab"
    )[0].split("function _laborDaily")[-1] if "function _laborDaily" in s else True


def test_the_toggle_rule_is_the_only_thing_that_can_color_the_pill():
    """Both pills share one rule, gated purely by the .on class — nothing
    else in the file may set a background on either of them directly."""
    s = _src()
    assert re.search(r"\.lb2-range button\.on\{background-color:var\(--hb-tint2\)", s)
    # no per-id override for either pill anywhere in the file
    assert "#perf-tab-trend{" not in s and "#perf-tab-dow{" not in s
    assert ".style.background" not in _fn("switchPerfTab") if "function switchPerfTab" in s else True


def test_the_ontoggle_pill_rules_use_the_longhand_not_the_shorthand():
    """background: var(--x) asks the browser to expand a shorthand whose
    value it can't classify until the variable resolves; background-color
    has no such ambiguity. All three toggle-pill rules in the file follow
    the same pattern — Labor's included — so all three get the same fix."""
    s = _src()
    for cls in (".hb-range", ".rv2-days", ".lb2-range"):
        assert (cls + " button.on{background-color:var(--hb-tint2)") in s, \
            cls + " still uses the background shorthand"
        assert (cls + " button.on{background:var(--hb-tint2)") not in s


# ── Bug 2: the fill color ──────────────────────────────────────────────────

def _branches(glowline_body):
    """glowLine's `if(hasTarget){...} else {...}` split, as two strings."""
    if_start = glowline_body.index("if(hasTarget){")
    else_marker = glowline_body.index("} else {", if_start)
    if_branch = glowline_body[if_start:else_marker]
    else_start = else_marker + len("} else {")
    else_end = glowline_body.index("\n    }", else_start)
    else_branch = glowline_body[else_start:else_end]
    return if_branch, else_branch


def test_glowline_paints_two_fills_when_a_target_is_given():
    body = _fn("glowLine")
    assert "hasTarget" in body
    if_branch, _ = _branches(body)
    # two distinct fill paths, one per side of the target line
    assert if_branch.count('class="hb-fill"') == 2
    assert "clip-path" in if_branch and "RED" in if_branch


def test_glowline_stays_a_single_fill_when_theres_no_target():
    """Every other caller (Home's value-delivered sparkline, Reviews'
    rating trend) never passes opts.target — they must keep the exact
    single-fill rendering they always had, untouched by this change."""
    body = _fn("glowLine")
    _, else_branch = _branches(body)
    assert else_branch.count('class="hb-fill"') == 1
    assert "clip-path" not in else_branch


def test_no_caller_other_than_labor_passes_a_target():
    """The whole point of gating the new fill-split behind opts.target is
    that it can never fire for a caller that doesn't ask for it."""
    s = _src()
    calls = []
    for m in re.finditer(r"glowLine\(", s):
        if s[max(0, m.start() - 9):m.start()] == "function ":
            continue  # the definition itself, not a call
        i = m.end() - 1  # at the opening '('
        depth = 0
        for j in range(i, len(s)):
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
                if depth == 0:
                    calls.append(s[m.start():j + 1])
                    break
    assert len(calls) == 4, "expected exactly the four known glowLine call sites: " + str(len(calls))

    def _enclosing_fn(pos):
        prev = list(re.finditer(r"function (\w+)\(", s[:pos]))
        return prev[-1].group(1) if prev else None

    non_labor = [c for c in calls if _enclosing_fn(s.index(c)) != "renderLaborHero"]
    assert len(non_labor) == 3
    for c in non_labor:
        assert "target:" not in c, "a non-Labor caller now opts into the red-split fill: " + c[:120]


# ── The tooltip and the callout ────────────────────────────────────────────

def test_labor_daily_carries_the_figures_the_tooltip_actually_shows():
    body = _fn("_laborDaily")
    for field in ("sales:", "labor_cost:", "actual:"):
        assert field in body, "tooltip would have nothing to show for " + field


def test_the_hover_tooltip_never_states_a_dollar_figure_it_doesnt_have():
    """The weekly-trend view only has {label, pct} — no sales/labor_cost/
    hours. Every $ row in the tooltip must stay conditional on the field
    actually being present, or the trend view would render '$0' rows for
    numbers nobody measured."""
    body = _fn("_wireLaborHover")
    for row in ("Sales", "Labor cost", "Hours"):
        assert ("if(p." in body), "tooltip row for %r isn't guarded" % row
    assert body.count("if(p.sales)") == 1
    assert body.count("if(p.labor_cost)") == 1
    assert body.count("if(p.actual)") == 1


def test_the_worst_day_callout_disappears_on_a_healthy_period():
    body = _fn("_laborCallout")
    assert "if(worstI<0)return '';" in body, \
        "a period with nothing over target must get no callout, not a made-up one"


def test_the_callout_only_names_a_day_that_is_actually_over_target():
    body = _fn("_laborCallout")
    # the running "worst" starts at target itself, so a day has to beat
    # target to ever become a candidate — never just the highest day period
    assert "worstV=target" in body


def test_the_callout_is_daily_only_not_the_coarser_weekly_view():
    body = _fn("_laborCallout")
    assert "if(trend||" in body


def test_hover_listeners_are_replaced_not_stacked_on_every_rerender():
    """renderLaborHero runs again on every tab switch / data reload. Without
    this, each render would leave its own mousemove listener behind,
    firing N times per pixel of mouse movement after N renders."""
    body = _fn("_wireLaborHover")
    assert "removeEventListener" in body
    assert "_lb2HoverBound" in body
