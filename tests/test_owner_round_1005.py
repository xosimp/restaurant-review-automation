"""The owner's layout calls of 10/5/26: Measured ratings sit right under the
Operational Score they feed (Studio → Team), and the Staffing review left the
Studio for the Labor page — under Time off and Covers, above Details."""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1].joinpath("templates", "dashboard.html").read_text(encoding="utf-8")


def _at(marker):
    i = SRC.find(marker)
    assert i != -1, marker
    return i


def test_measured_ratings_come_right_after_the_operational_score():
    score = _at('<div class="lb2-subsection-title">Operational Score <span id="team-coverage-chip"')
    ratings = _at('<div class="lb2-subsection-title">Measured ratings <span id="sfw1-rate-chip"')
    rules = _at('<div class="lb2-subsection-title">Scheduling rules <span id="rules-chip"')
    assert score < ratings < rules
    between = SRC[score:ratings]
    assert between.count('class="lb2-subsection-title"') == 1, "nothing else sits between them"
    assert SRC.count('id="sfw1-rate-row"') == 1


def test_the_staffing_review_is_on_labor_under_time_off_and_covers():
    timeoff = _at('<section class="lb2-op" id="lb2-timeoff"')
    covers = _at('<section class="lb2-op" id="lb2-covers"')
    staffing = _at('<details class="hb-results lb2-money-all" id="lb2-money-all"')
    details = _at('<details class="hb-results lb2-details" id="lb2-details"')
    assert timeoff < covers < staffing < details
    assert SRC.count('id="lb2-money-all"') == 1
    assert _at("{% set _sb = labor.staffing_board if labor.is_live else None %}") < staffing, \
        "the board it renders is set just above it"


def test_the_studio_has_no_staffing_stage_any_more():
    assert 'data-ss-go="staffing"' not in SRC
    assert 'data-stage="staffing"' not in SRC and 'id="ss-staffing"' not in SRC
    assert "var SS_STAGES = ['setup', 'build', 'summary', 'schedule', 'publish', 'history', 'team'];" in SRC
    assert "'staffing'" not in SRC[_at("var SS_STAGES"):_at("var SS_STAGES") + 4000]


def test_a_target_box_saves_once_it_settles_not_on_every_spinner_step():
    """Simple EJ's 35% labor target became 31.5% in seven saves in two
    seconds from the Studio's box (10/2/26): each spinner step was a save."""
    helper = SRC[_at("window.cavSettled = function (el, fn) {"):]
    helper = helper[:helper.index("\n};") + 3]
    assert "el.type !== 'number'" in helper and "setTimeout(" in helper and "800" in helper
    assert "el._cavSettleFn = fn;" in helper and "addEventListener('blur'" in helper
    studio = SRC[_at("  if (t.id === 'sw-target') {"):]
    studio = studio[:studio.index("\n  }") + 4]
    assert "cavSettled(t, function () {" in studio and "_swSave({labor_target_pct: v}" in studio
    assert "your labor target is now" in studio and "everywhere" in studio, "it says the target is the restaurant's"
    acct = SRC[_at("if (f && body[f] === '') { status('as-tg-status', 'Enter a number', true); return; }"):]
    assert acct.index("cavSettled(t, function () {") < acct.index("postJ('/api/account/targets', body")
