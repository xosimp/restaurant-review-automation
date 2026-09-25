"""Owner-reported UI fixes, 9/25/26 — each pinned against the source so a
later edit can't quietly undo it."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_no_on_screen_not_for_us_label():
    assert ">Not for us<" not in SRC and 'aria-label="Not for us"' not in SRC


def test_focus_card_puts_the_primary_beside_could_also_be_and_answers_after():
    body = SRC[SRC.index("  function renderFocus(d,g){"):SRC.index("  // ── action lines")]
    assert body.index("Could also be") < body.index("cbtn-primary") < body.index("h+=fAns;") < body.index("Ask about this")


def test_a_caution_that_repeats_the_reason_is_not_drawn_twice():
    assert "function cfSameHead(a,b)" in SRC and "cfSameHead(n.reason,n.caution)" in SRC


def test_the_day_heading_is_the_weekday():
    assert "<h2>The day <small>" not in SRC and "weekdayName(d.local_now)" in SRC


def test_before_service_actions_sit_after_the_answers_behind_a_rule():
    assert "hb-tl-acts" in SRC and "'<span class=\"sep\" aria-hidden=\"true\"></span>'" in SRC


def test_the_value_chart_has_room_under_its_text():
    m = re.search(r"\.hb-hero svg\{[^}]*margin-top:(\d+)px", SRC)
    assert m and int(m.group(1)) >= 24


def test_the_ai_strip_orbit_follows_the_outline_not_a_spinning_cone():
    assert "conic-gradient(from 0deg,var(--paper3) 0 55%" not in SRC
    assert '<svg class="orbit"' in SRC and "function aiOrbitSize()" in SRC


def test_reviews_has_no_section_rail_and_names_analytics_in_its_kicker():
    rail = SRC[SRC.index("function cavRailBuild(panel) {"):]
    assert "panel.id === 'panel-reviews'" in rail[:600]
    assert 'data-nav-go="reviews/analytics">Analytics</button>' in SRC


def test_a_hidden_empty_state_stays_hidden_and_the_filter_drives_it():
    assert ".pe[hidden]{display:none!important}" in SRC
    f = SRC[SRC.index("function filterReviews(){"):SRC.index("function rvAnsweredDivider(){")]
    assert "emp.hidden=shown>0" in f
