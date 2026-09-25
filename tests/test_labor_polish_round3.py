"""A third pass on the Labor page, from a fresh screenshot + live-profiling
pass:

- The Overtime alerts card still read as the exact same table as
  Overstaffed days even after round 2's border/text-color fix — the two
  cards share `.dark-hero-card`'s background gradient, and a 1px border
  color difference doesn't register next to an identical near-black fill.
  It also sat a few pixels lower than Overstaffed days: its heading
  wrapper used `margin-bottom:8px` where Overstaffed's used 4px.
- Employee Availability / Operational Score's expanded panel used a flat
  `var(--hb-tint)` wash with no border — a single-opacity tint over
  obsidian black reads as an undefined brown smear, not a branded card.
- Chart/number reveal animations looked "stuck" briefly on their first
  scroll-in. Live instrumentation (IntersectionObserver latency,
  PerformanceObserver longtasks, rAF frame gaps, and — the actual
  finding — document.hidden sampled mid-scroll) showed the trigger code
  itself runs in under 1ms; the stall lines up with the OS marking the
  tab briefly occluded during the scroll gesture, which pauses
  requestAnimationFrame entirely. Every count-up in this file used
  wall-clock elapsed time, so the next tick after such a pause jumps
  straight to wherever real elapsed time says the count should be —
  looking exactly like "stuck, then snaps forward."
- The worst-day callout bubble on the labor ribbon chart can sit as high
  as top:2% of the chart before flipping above its own anchor point,
  and only 6px separated the chart from the "% of sales" number above
  it — enough for the bubble to crowd that number.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _fn(name):
    s = _src()
    m = re.search(r"function %s\(" % re.escape(name), s)
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


# ── Overtime alerts finally reads as a distinct, red table ──────────────────

def test_overtime_card_has_its_own_dark_background_not_the_orange_shared_one():
    css = _src()
    assert '[data-theme="dark"] .dark-hero-card.ot-hero-card{background:' in css
    m = re.search(r'\[data-theme="dark"\] \.dark-hero-card\.ot-hero-card\{background:([^!]+)!important\}', css)
    assert m
    orange_stops = ("#1a0f0a", "#120c08", "#0e0a06")
    assert not any(stop in m.group(1) for stop in orange_stops), \
        "overtime's background should not reuse the overstaffed gradient's exact color stops"


def test_overtime_heading_matches_overstaffed_structure_for_alignment():
    """Density round #44: both lists are hb-cards whose heading is the same
    .lb2-ma-h line, so neither card's top edge can drift from the other."""
    s = _src()
    assert '<div class="lb2-ma-h">Overstaffed days<small>' in s
    assert '<div class="lb2-ma-h">Overtime alerts<small>' in s


# ── Employee Availability / Operational Score panel looks branded ──────────

def test_avail_panel_uses_a_gradient_and_border_not_a_flat_tint():
    css = _src()
    m = re.search(r"(?<!\.)\.lb2-avail\{([^}]*)\}", css)
    assert m, "missing .lb2-avail rule"
    rule = m.group(1)
    assert "var(--hb-tint)" not in rule, "still the flat single-opacity wash"
    assert "linear-gradient" in rule and "border:1px solid" in rule


# ── scroll-reveal animations no longer snap forward after a paused tab ─────

def test_shared_count_up_folds_rAF_gaps_instead_of_snapping():
    s = _src()
    i = s.index("function countUp(el, target, duration, prefix, suffix, decimals)")
    body = s[i:s.index("\n}\n", i)]
    assert "if (gap > 100) startTime += gap;" in body
    body_money = _fn("countUpMoney")
    assert "if (gap > 100) startTime += gap;" in body_money


def test_donut_total_tween_folds_rAF_gaps_too():
    body = _fn("renderRoleDonut")
    assert "if(gap>100)start+=gap;" in body.replace(" ", "")


def test_home_hero_value_delivered_count_up_folds_rAF_gaps_too():
    s = _src()
    i = s.index("function countUp(el,target,prefix){")
    j = s.index("\n  }\n", i)
    body = s[i:j]
    assert "if(gap>100)start+=gap;" in body.replace(" ", "")


# ── the worst-day callout no longer crowds the header number above it ──────

def test_hero_chart_container_has_headroom_for_the_callout_bubble():
    css = _src()
    m = re.search(r"#lb2-hero-chart\{([^}]*)\}", css)
    assert m, "missing #lb2-hero-chart rule"
    mt = re.search(r"margin-top:(\d+)px", m.group(1))
    assert mt and int(mt.group(1)) >= 20, \
        "not enough headroom between the '% of sales' number and the chart/callout below it"
