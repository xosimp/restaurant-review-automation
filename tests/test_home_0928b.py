"""Home and Reviews, 9/28/26 evening (owner): Google's rating on the Home
chip, no data line under Reviews, no "Draft a post", a named slow-night
action, one tooltip on the Labor chart, and a drawn empty state for the
results curve."""
import inspect
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_home_chip_shows_googles_rating_when_on_file():
    import home_brief
    assert '"google_rating": (round(float(r["gbp_rating"]), 1) if r.get("gbp_rating") else None)' in \
        inspect.getsource(home_brief)
    fn = SRC[SRC.index("  function renderTop(d){"):SRC.index("    p.innerHTML=chips;")]
    assert "s.google_rating!=null" in fn and "'Google rating'" in fn


def test_reviews_has_no_data_line_and_home_no_draft_a_post():
    assert 'data-dh-module="reviews"' not in SRC
    import home_brief
    assert '"label": "Draft a post"' not in inspect.getsource(home_brief)


def test_the_slow_night_action_says_where_it_goes():
    import morning_brief
    src = inspect.getsource(morning_brief)
    assert '"label": "See the card"' not in src and "f\"Ideas to fill {d['day']}s\"" in src


def test_the_labor_chart_shows_one_tooltip():
    hover = SRC[SRC.index("  function hbChartHover(e){"):SRC.index("  document.addEventListener('mousemove',hbChartHover);")]
    assert "svg.closest('[data-own-tip]')" in hover
    assert "el.setAttribute('data-own-tip','1');" in SRC and "[data-own-tip] .hb-xh{display:none}" in SRC


def test_the_results_curve_has_a_drawn_empty_state():
    assert "else h+=hbHeroEmpty();" in SRC
    fn = SRC[SRC.index("  function hbHeroEmpty(){"):]
    fn = fn[:fn.index("\n  }\n")]
    assert 'class="hb-ghost"' in fn and "/static/brand/seal-color.svg" in fn and "Your measured results draw here" in fn
    assert "@media (prefers-reduced-motion:reduce){.hb-hero-empty .hb-ghost .gc{animation:none}}" in SRC


def test_kpi_cards_span_their_row_and_sparklines_stay_inside():
    assert ".dr-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))" in SRC
    assert ".dr-kpis{display:grid;grid-template-columns:repeat(auto-fill" not in SRC
    assert ".dr-spark{color:var(--ink3);flex:0 1 84px;min-width:36px;height:auto}" in SRC
