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
    # 9/25/26 redesign: the three lists are lanes of one staffing board,
    # every lane headed by the same .sb-lh row, so no lane can drift.
    s = _src()
    assert "('over', 'Overstaffed days'" in s and "('ot', 'Overtime'" in s
    assert '<div class="sb-lh">' in s and s.count('<div class="sb-lh">') == 1   # one template, three lanes


# ── Employee Availability / Operational Score panel looks branded ──────────

def test_avail_panel_uses_a_gradient_and_border_not_a_flat_tint():
    css = _src()
    m = re.search(r"(?<!\.)\.lb2-avail\{([^}]*)\}", css)
    assert m, "missing .lb2-avail rule"
    rule = m.group(1)
    assert "var(--hb-tint)" not in rule, "still the flat single-opacity wash"
    assert "linear-gradient" in rule and "border:1px solid" in rule


# ── scroll-reveal animations no longer snap forward after a paused tab ─────

def test_every_count_up_is_the_one_engine():
    """Five tweens each had their own curve and stall handling; the Intel
    percentage lagged, then raced (owner, 9/26/26). One engine now."""
    s = _src()
    i = s.index("function countUp(el, target, duration, prefix, suffix, decimals)")
    assert "cavCount(el, target," in s[i:s.index("\n}\n", i)]
    assert "cavCount(el, target, {duration: duration, prefix: '$'})" in _fn("countUpMoney")
    assert "cavCount(totalEl,total,{fmt:_fmtK})" in _fn("renderRoleDonut")
    i = s.index("function countUp(el,target,prefix){")
    assert "cavCount(el,Math.round(target),{prefix:prefix})" in s[i:s.index("\n  }\n", i)]
    # No private tween left: no other gap-fold, no cubic ease on a number.
    assert "gap>100" not in s.replace(" ", "") and "Math.pow(1-p,3)" not in s.replace(" ", "")


def test_the_engine_pauses_through_a_stall_and_never_jumps():
    import json, shutil, subprocess
    import pytest
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    s = _src()
    i = s.index("function cavCount(el, target, o) {")
    eng = s[i:s.index("\nwindow.cavCount = cavCount;", i)]
    js = """
var q=[], now=0, seen=[];
var window={matchMedia:function(){return {matches:false};}};
var document={hidden:false};
function requestAnimationFrame(f){q.push(f);}
function setTimeout(){}
function getComputedStyle(){return {display:'inline'};}
var el={style:{},getBoundingClientRect:function(){return {width:40};},
  set textContent(v){seen.push(v);this._t=v;}, get textContent(){return this._t;}};
""" + eng + """
cavCount(el, 100, {suffix:'%'});
var frames=0;
while(q.length && frames<2000){var f=q.shift(); now += (frames===30 ? 600 : 16); frames++; f(now);}
console.log(JSON.stringify({seen:seen, frames:frames}));
"""
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip())
    vals = [int(v.rstrip("%")) for v in got["seen"][2:]]      # after the width reservation
    assert vals[-1] == 100 and got["seen"][-1] == "100%"
    assert all(b >= a for a, b in zip(vals, vals[1:]))         # never backwards
    assert max(b - a for a, b in zip(vals, vals[1:])) <= 4       # a 600ms stall is not a jump
    assert got["frames"] >= 80                                   # long enough to be seen (~1.5s+)


# ── the worst-day callout no longer crowds the header number above it ──────

def test_hero_chart_container_has_headroom_for_the_callout_bubble():
    css = _src()
    m = re.search(r"#lb2-hero-chart\{([^}]*)\}", css)
    assert m, "missing #lb2-hero-chart rule"
    mt = re.search(r"margin-top:(\d+)px", m.group(1))
    assert mt and int(mt.group(1)) >= 20, \
        "not enough headroom between the '% of sales' number and the chart/callout below it"
