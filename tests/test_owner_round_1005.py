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
    assert "var SS_STAGES = ['setup', 'build', 'summary', 'schedule', 'publish', 'history', 'crew', 'team'];" in SRC
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
    assert 'canvas class="hb-orb"' in rep[:rep.index("\n")] and 'width="96"' in rep[:rep.index("\n")]
    assert ".dr-page-load{height:300px;justify-content:center;flex-direction:column" in SRC
    assert ".dr-page-load span{font-size:16px;" in SRC
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
    # The night is named in the report link itself (owner, 10/9/26).
    assert "dayTag==='Yesterday'?'Last night’s report'" in st


def test_the_late_night_hour_is_the_owners_to_set():
    row = SRC[_at('<select class="ac-select" id="as-dsr-late"'):]
    row = row[:row.index("</select>")]
    assert "saveDsrSetting('dsr_late_night_hour')" in row and '<option value="">Off</option>' in row
    assert all('<option value="%d">' % h in row for h in range(18, 24))
    assert "late.value = s.dsr_late_night_hour == null ? '' : String(s.dsr_late_night_hour);" in SRC
    assert "else if (field === 'dsr_late_night_hour') { v = document.getElementById('as-dsr-late').value; v = v === '' ? null : parseInt(v, 10); }" in SRC


def test_before_service_lists_the_today_items_and_keeps_orange_for_what_matters():
    sig = SRC[_at("  function briefSig(t){"):]
    sig = sig[:sig.index("\n  }\n")]
    assert "return +m[1]>=100;" in sig and "return +m[2]>=5;" in sig
    assert "l.rows&&l.rows.length?briefNum(l.head||'')+'<span class=\"hb-tl-rows\">'" in SRC
    assert ".hb-tl .t .hb-num{color:var(--ink)}" in SRC and ".hb-tl .t .hb-num.sig{color:var(--ember)}" in SRC
    assert ".hb-tl .t .hb-num,.hb-rec" not in SRC, "brief numbers are no longer all ember"


def test_the_manager_toggles_clear_the_rule_above_them():
    # c69687da (10/6/26) took the rule away instead: the row sits under its
    # person with the same spacing either side of the switches.
    assert "row+='<div class=\"ac-team-access\" style=\"display:flex;flex-wrap:wrap;gap:12px 22px;padding:8px 0 18px\">'+bits.join('')+'</div>';" in SRC


def test_every_helper_is_defined_in_the_script_block_that_calls_it():
    """briefNum/briefSig were defined in the cav-conf script and called from
    Home's: a ReferenceError, and Home stopped drawing at Before service for
    every owner (Simple EJ's, 10/6/26). The string asserts above passed."""
    import re
    blocks = [(m.start(), m.end()) for m in re.finditer(r"<script[^>]*>.*?</script>", SRC, re.S)]

    def block(i):
        return next(k for k, (a, b) in enumerate(blocks) if a <= i < b)
    for name in ("briefNum", "briefSig", "hbIssuesNotShown", "hbReportDay", "_firstInitial", "mktOppFocus",
                 "sfw2StandingLabel", "_dsrRenderCats", "glowLine", "hbChartHover", "_ssSavingsMath",
                 "rvSetState", "rvClearState", "renderScheduleEta", "scheduleTiming"):
        if re.search(r"window\." + name + r"\s*=", SRC):
            continue
        defs = {block(m.start()) for m in re.finditer(r"function " + name + r"\(", SRC)}
        assert defs, name
        for m in re.finditer(r"(?<![\w.])" + name + r"\(", SRC):
            assert block(m.start()) in defs, "%s called at line %d outside the script that defines it" % (
                name, SRC.count("\n", 0, m.start()) + 1)


def test_estimated_savings_shows_its_math_on_hover_or_focus():
    """Owner, 10/6/26: "a tooltip with the math behind where that # was
    derived from so owners know". The server's own figures, one formula."""
    fn = SRC[_at("function _ssSavingsMath(lv, rev) {"):]
    fn = fn[:fn.index("\n}\n")]
    assert "lv.savings == null || lv.recent_pct == null" in fn, "no figure, no card"
    assert "'Forecast sales this week'" in fn and "n(_schedMoney(r / 100 * R))" in fn and "n(_schedMoney(p / 100 * R))" in fn
    assert "' = ' + n(pts + ' pts') + ' \\u00d7 ' + n(_schedMoney(R)) + ' = <b>' + n(_schedMoney(S))" in fn
    assert "'<span class=\"tag\">Projection · not yet earned</span>' + _ssSavingsMath(lv, rev));" in SRC
    assert "(extra && extra.indexOf('ss-math') > -1 ? ' has-math\" tabindex=\"0' : '')" in SRC
    assert ".ss-tile.has-math:hover .ss-math,.ss-tile.has-math:focus-within .ss-math{opacity:1;visibility:visible;" in SRC


def test_marketing_loads_with_the_orb_on_every_tab_and_the_text_card_runs_full_height():
    """Owner, 10/6/26: the orb, big enough to notice, in place of the orange
    line across Content, Campaigns, Scheduled and Analytics; the text card
    as tall as the email card beside it."""
    panel = SRC[_at('<div class="panel" id="panel-marketing"'):]
    panel = panel[:panel.index('id="mkt-attr-card"')]
    assert "dr-pulse" not in panel
    assert "window.mktLoading=function(label,size,state){size=size||48;" in SRC
    for fn in ("function loadMktOpps(", "function loadEmailHistory(", "function _loadGuestContactsList(", "function cpLoadMedia("):
        body = SRC[_at(fn):][:1200]
        assert "dr-pulse" not in body and "mktLoading(" in body, fn
    assert ".cp-chs:has(> #cp-ch-social[hidden])>#cp-ch-text{grid-row:1/span 2;align-self:stretch;" in SRC


def test_done_for_you_cards_put_their_category_bottom_right():
    assert ".hb-rcpt .it{position:relative;padding-bottom:36px}" in SRC
    assert ".hb-rcpt .m{position:absolute;right:16px;bottom:13px;margin:0;" in SRC


def test_the_studio_day_row_is_one_full_width_band_with_no_blur():
    """Owner, 10/6/26: the boxed black cells read tacky against the page -
    one band edge to edge. 10/9/26: "no blur" - no fade and no darker
    shade, a hard-edged band in the page's own colour."""
    assert ".ss .swg-hr>.swg-d{background:transparent;border-radius:0}" in SRC
    band = SRC[_at(".ss .swg-hr>.swg-d:first-child::before{"):]
    band = band[:band.index("}")]
    assert "left:-100vw;right:-100vw" in band and "background:var(--paper)" in band
    assert "transparent" not in band and "--ss-band" not in band
    assert ".ss .ss-main{overflow-x:hidden}" in SRC


def test_setup_never_lands_on_an_empty_build_stage():
    """Owner, 10/9/26: History, open a week, back to Setup showed a blank
    canvas. Reopening a week clears _schedBtn, so a generation's own answer
    read as a reopen and the Studio's "generating" flag stayed on; Setup then
    redirected to a Build stage with nothing building. The job's answer now
    ends it, and Build counts only while the Building visual exists."""
    assert "pollData._fromJob = true;" in SRC
    assert "studioOnResult(data, !!_schedBtn || !!data._fromJob)" in SRC
    fn = SRC[_at("function ssBuilding() {"):]
    fn = fn[:fn.index("\n}")]
    assert "!document.getElementById('sched-week')" in fn and "_ssGenerating = false" in fn
    go = SRC[_at("function studioGo(stage) {"):]
    go = go[:go.index("if (stage === 'setup' && _ssGenerating)")]
    assert "ssBuilding();" in go


def test_the_bell_refresh_lands_in_place_and_never_replays_the_rows():
    """Owner, 10/9/26: opening the bell flickered. It drew the cached rows
    with their entrance, then the fresh read replaced them while .hp-enter
    was still on, so every row faded in twice. Same rows: DOM untouched;
    changed rows: swapped without the entrance."""
    fn = SRC[_at("  function renderList() {"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "if (list._nbHtml !== html) {" in fn
    assert "if (!_animate) list.classList.remove('hp-enter');" in fn
    assert fn.index("list._nbHtml !== html") < fn.index("list.innerHTML = html;")


def test_the_studio_right_column_collapses_and_reopens():
    """Owner, 10/9/26: a manager can hide the right column to give the
    schedule the full width, and reopen it from a slim strip."""
    assert 'onclick="ssRightCollapse(true)"' in SRC and 'onclick="ssRightCollapse(false)"' in SRC
    assert ".ss .sw-grid.ss-rcollapsed{grid-template-columns:minmax(0,1fr) 46px" in SRC
    fn = SRC[_at("function ssRightCollapse(on) {"):]
    fn = fn[:fn.index("\n}")]
    assert "localStorage.setItem('cavnar_ss_right'" in fn and "try {" in fn
