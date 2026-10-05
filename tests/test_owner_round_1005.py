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


def _fn(name):
    i = _at("function " + name + "(")
    return SRC[i:SRC.index("\n  }\n", i)]


def test_a_glowline_draws_round_dots_and_even_strokes_at_any_width():
    """The svg stretches (preserveAspectRatio none): circles drew as ovals
    and strokes thickened on slopes (owner, 10/5/26, Intel's rating chart)."""
    fn = _fn("glowLine")
    assert "<circle" not in fn, "every dot is HTML over the chart"
    assert fn.count('vector-effect="non-scaling-stroke"') >= 4    # line, glow, target, crosshair
    assert "hb-cl-pt hb-hot" in fn and "hb-cl-pt hb-dot hb-cl-end" in fn and 'class="hb-cl-xd"' in fn
    assert "(p[0]/w*100)" in fn and "(p[1]/h*100)" in fn, "placed by percentage, so they track the stretch"
    assert "+gl+dots+tgtLbl+" in fn
    hover = _fn("hbChartHover")
    assert "querySelector('.hb-cl-xd')" in hover and "d.style.left=" in hover and "setAttribute('cx'" not in hover
    assert ".hb-cl-end{width:8px;height:8px;" in SRC and ".hb-cl:hover .hb-cl-xd{opacity:1}" in SRC


def test_the_rating_chart_says_nothing_under_it_when_the_rating_did_not_move():
    i = _at("window.memIntelHistoryHtml = function (d) {")
    body = SRC[i:SRC.index("\n  };", i)]
    assert "(ch ? '<div class=\"mem-ln\">' + memNum(memOwnRatingLine(own)) + '</div>' : '')" in body


def test_what_changed_and_over_the_months_are_headings_not_kickers():
    assert '<h3 class="in2-subh">What changed</h3>' in SRC
    assert '<h3 class="in2-subh">Over the months</h3>' in SRC
    assert "hb-kicker\" style=\"margin-top:22px\">What changed" not in SRC
    rule = SRC[_at(".in2-subh{"):]
    rule = rule[:rule.index("}")]
    assert "Clash Display" in rule and "font-size:19px" in rule and "color:var(--ink)" in rule


def test_the_website_and_review_trend_tips_name_their_day():
    web = SRC[_at("function mktWebsiteHtml(d){"):]
    web = web[:web.index("\n}\n")]
    assert "days.push(mdy(line[i].day))" in web and "labels: days" in web
    assert "labels:weeks.map(function(w){return 'Week of '+lab(w);})" in SRC


def test_the_studio_step_rule_has_the_same_space_either_side():
    nav = SRC[_at('<nav class="ss-steps" role="tablist" aria-label="Schedule Studio steps">'):]
    nav = nav[:nav.index("</nav>")]
    assert nav.index('data-ss-go="publish"') < nav.index('class="ss-steps-sep"') < nav.index('data-ss-go="history"')
    assert "ss-steps-h" not in SRC, "the rule is no longer drawn on History's edge"
    assert ".ss-steps .ss-steps-sep{flex:none;width:1px;height:18px;margin:0 6px;" in SRC


def test_round_three_layout_calls():
    # The Studio's header: no "← Dashboard" and no rule beside it.
    assert "&larr; Dashboard" not in SRC and "ss-back" not in SRC
    brand = SRC[_at(".ss-brand{"):]
    assert "box-shadow" not in brand[:brand.index("}")]
    # Reports load with the orb at page scale.
    rep = SRC[_at("  function loading(label){return '<div class=\"hb-load dr-page-load\""):]
    assert 'canvas class="hb-orb"' in rep[:rep.index("\n")] and 'width="56"' in rep[:rep.index("\n")]
    assert ".dr-page-load{height:260px;justify-content:center;flex-direction:column" in SRC
    # The recovery email's check, address and Remove sit on one centred line.
    assert '<span class="rec-line"><i class="cv-ok" aria-hidden="true"></i><span>' in SRC
    assert "#rec-status .rec-line{display:flex;align-items:center;" in SRC
    # Staff not signed up: first name and last initial, full names on a clash.
    un = SRC[_at("  function _renderUnclaimed(names){"):]
    un = un[:un.index("\n  function _phone(")]
    assert "names.map(_firstInitial)" in un and "seen[short[j]]>1?n:short[j]" in un
    assert "p[0]+' '+p[1].charAt(0).toUpperCase()+'.'" in un and "font-size:11px" not in un


def test_home_says_which_night_the_report_line_is():
    fn = SRC[_at("  function hbReportDay(bd,tonight){"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "if(diff===1)return 'Yesterday';" in fn and "if(diff===0)return 'Today';" in fn
    assert "var now=tonight||" in fn and "_hbData.local_now" in fn, "the restaurant's own date, not the browser's"
    st = SRC[_at("  function hbStatusHtml(x){"):]
    st = st[:st.index("\n  }\n")]
    assert "var dayTag=hbReportDay(r.business_date,x.tonight);" in st
    assert "(dayTag?'<span class=\"day\">'+esc(dayTag)+'</span>':'')+bits.join(" in st
    assert ".hb-status .day{display:inline-flex;" in SRC


def test_the_late_night_hour_is_the_owners_to_set():
    row = SRC[_at('<select class="ac-select" id="as-dsr-late"'):]
    row = row[:row.index("</select>")]
    assert "saveDsrSetting('dsr_late_night_hour')" in row and '<option value="">Off</option>' in row
    assert all('<option value="%d">' % h in row for h in range(18, 24))
    assert "late.value = s.dsr_late_night_hour == null ? '' : String(s.dsr_late_night_hour);" in SRC
    assert "else if (field === 'dsr_late_night_hour') { v = document.getElementById('as-dsr-late').value; v = v === '' ? null : parseInt(v, 10); }" in SRC
