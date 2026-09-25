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


def test_no_module_draws_the_section_rail_and_reviews_names_analytics_in_its_kicker():
    assert "cavRailBuild" not in SRC and "cav-rail" not in SRC
    assert 'data-nav-go="reviews/analytics">Analytics</button>' in SRC


def test_a_hidden_empty_state_stays_hidden_and_the_filter_drives_it():
    assert ".pe[hidden]{display:none!important}" in SRC
    f = SRC[SRC.index("function filterReviews(){"):SRC.index("function rvAnsweredDivider(){")]
    assert "emp.hidden=shown>0" in f


def test_where_the_money_went_ranks_every_kind_by_dollars():
    """It listed up to three overstaffed days, then up to three overtime
    people, unranked; now one list across every priced kind, most first."""
    import labor
    a = {"overstaffed_days": [{"day": "Tuesday", "date": "2026-09-15", "labor_pct": 31, "sales": 4000,
                               "over_target_dollars": 60}],
         "overtime_risk": [{"employee": "Marcus R.", "status": "overtime", "hours": 59.5, "week": "9/7/26",
                            "premium": 88},
                           {"employee": "Devon K.", "status": "near", "hours": 38, "week": "9/7/26", "premium": 500}],
         "employee_hours": {"Angela M.": {"scheduled": 30, "actual": 36.5}},
         "hours_are_estimated": False}
    got = labor.money_went(a, rate=15.0)
    assert [(g["kind"], g["dollars"]) for g in got] == [("past_schedule", 98.0), ("overtime", 88.0),
                                                        ("overstaffed", 60.0)]
    # Estimated hours carry no clocked-vs-scheduled comparison.
    a["hours_are_estimated"] = True
    assert [g["kind"] for g in labor.money_went(a, rate=15.0)] == ["overtime", "overstaffed"]


def test_the_labor_top_says_its_dates_once():
    assert 'data-dh-module="labor" data-dh-compact="1"' in SRC
    assert '<div class="dh-badge" data-dh-module="labor"' not in SRC
    assert 'id="lb2-today"' not in SRC


def test_the_one_thing_waits_for_its_lead_instead_of_promoting_an_attention_item():
    assert "function renderFocusPending()" in SRC and "h+=renderFocus(d,null);" not in SRC


def test_the_modal_big_number_glow_is_turned_down():
    import re
    m = re.search(r"\.cf-p-big\{[^}]*text-shadow:([^;}]*)", SRC)
    assert m and "22px" not in m.group(1)
