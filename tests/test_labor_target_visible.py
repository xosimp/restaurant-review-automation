"""The labor target reads beside the actual, and the chart's target line
carries a legible label (owner, 9/28/26)."""
import os
import re

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html"),
           encoding="utf-8").read()


def test_the_hero_shows_the_target_across_a_hairline_from_the_labor_pct():
    i = SRC.index('<span class="hb-num pct stat-n {{ \'good\' if _lp <= _lt else \'bad\' }}">{{ _lp }}%</span>')
    assert SRC[i:i + 400].count('<span class="lb2-vs"') == 1 and "target</span>" in SRC[i:i + 400]
    assert re.search(r"\.lb2-bn \.lb2-vs\{[^}]*border-left:1px solid var\(--hb-line\)", SRC)


def test_the_target_label_is_html_over_the_chart_not_stretched_svg_text():
    fn = SRC[SRC.index("function glowLine("):]
    fn = fn[:fn.index("\n  }\n")]
    assert "<text" not in fn, "SVG text in a preserveAspectRatio=none chart is squashed"
    assert 'class="hb-cl-target"' in fn and "ty/h*100" in fn
    rule = re.search(r"\.hb-cl-target\{([^}]*)\}", SRC).group(1)
    assert "left:0" in rule and "background:var(--surface)" in rule and "color:var(--ink2)" in rule
