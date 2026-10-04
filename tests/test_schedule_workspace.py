"""The schedule workspace (owner, 9/26/26): generation settings on the left,
the week in the middle, how it scores on the right. Source rules, because
the layout must hold for every draft, not one fixture's."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name):
    s = _src()
    i = s.index("function " + name + "(")
    j = s.index("\n}\n", i)
    return s[i:j]


def _kickers(block):
    return re.findall(r'<div class="sw-k">([^<]+)</div>', block)


def test_three_panels_in_order_with_the_asked_for_sections():
    s = _src()
    ws = s[s.index('id="sched-ws"'):s.index('data-stage="publish"')]
    # The settings live only in Setup (owner, 9/26/26): the week gets the
    # width, and nothing is shown twice.
    setup = s[s.index('data-stage="setup"'):s.index('data-stage="build"')]
    left = setup[setup.index('id="ss-settings"'):]
    assert 'id="ss-settings"' not in ws and "studioRail" not in s
    assert ".ss .sw-grid{grid-template-columns:minmax(0,1fr) 400px;" in s
    right = ws[ws.index('id="sw-right"'):]
    # Settings as tabs, not every setting at once (owner, 9/26/26).
    assert re.findall(r'data-ss-tab="(\w+)"', left) == ["basic", "forecast", "employees", "rules", "ai", "advanced"]
    assert re.findall(r'data-ss-panel="(\w+)"', left) == ["basic", "forecast", "employees", "rules", "ai", "advanced"]
    assert 'id="gen-sched-btn"' in left[left.index('data-ss-panel="advanced"'):], "Generate closes the settings"
    # "What this draft was asked to do": the soft requirements and pattern
    # clashes (memory round 9/29/26, UI wave B), in the Overview after Labor.
    # "Shift requirements": who each shift was written and scored for, and why
    # a number moved (schedule fix round 10/3/26, UI W1), before Cost.
    assert _kickers(right) == ["Shift quality", "Coverage", "Labor", "What this draft was asked to do",
                               "Shift requirements", "Cost",
                               "Warnings", "Opportunities", "AI suggestions", "Apply fixes"]
    assert re.findall(r'data-ss-rtab="(\w+)"', right) == ["overview", "fix", "shift"]
    center = ws[ws.index('class="sw-center"'):ws.index('id="sw-right"')]
    for part in ('id="schedule-preview-panel"', 'id="swg"', 'id="sched-table-wrap"', 'data-sw-view="week"',
                 'data-sw-view="list"', 'id="sched-reopen"'):
        assert part in center


def test_the_studio_is_a_full_page_application_at_its_own_address():
    s = _src()
    assert ".ss{position:fixed;inset:0;z-index:950;" in s
    assert "body.ss-open{overflow:hidden}" in s
    assert "window._SS_BOOT={{ 'true' if request.path == '/schedule/studio' else 'false' }}" in s
    assert "history.pushState({ss: 1}, '', '/schedule/studio')" in s
    assert "if (st.parentNode !== document.body) document.body.appendChild(st);" in s
    stages = re.findall(r'<section class="ss-stage" data-stage="(\w+)"', s)
    # Team & rules and the Staffing review joined as stages (owner, 10/2/26).
    assert stages == ["setup", "build", "summary", "schedule", "publish", "history", "team", "staffing"]
    # Read, not imported: importing hosted_dashboard installs its CSRF hooks
    # on the shared blueprints for every later test in the process.
    hd = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    assert '@app.route("/")\n@app.route("/schedule/studio")\n@login_required\ndef index(current_user):' in hd
    assert "#panel-labor,#studio{--hb-line:" in s, "the studio keeps Labor's tokens at the page root"


def test_the_building_animation_is_kept_and_fills_the_build_stage():
    body = _src()[_src().index("function generateSchedule("):]
    body = body[:body.index("fetch('/api/generate-schedule'")]
    assert "cavnarWeekHtml(_wkLabel)" in body
    assert "document.getElementById('ss-build-host') || document.getElementById('sw-center')" in body
    assert "_swCtr.insertBefore(_wk, _swCtr.firstChild)" in body and "studioOnGenerate()" in body


def test_a_fresh_draft_lands_on_the_summary_and_a_reopened_one_on_the_week():
    s = _src()
    assert "if (window.studioOnResult) studioOnResult(data, !!_schedBtn);" in s
    f = _fn("studioOnResult")
    assert "studioGo('summary')" in f and "studioGo('schedule')" in f


def test_the_summary_states_its_six_figures_and_labels_savings_a_projection():
    f = _fn("ssRenderSummary")
    for k in ("'Shift quality'", "'Labor'", "'Coverage'", "'Overtime'", "'Warnings'", "'Estimated savings'"):
        assert k in f
    # The savings are the server's, on one basis with the draft's labor %
    # (schedule re-audit 10/4/26 SQ-4): the page's own "rec.pct > lp" put an
    # all-in recent % against the hourly-only draft, and the "savings" were
    # the salaries.
    assert "Projection · not yet earned" in f and "lv.savings" in f and "rec.pct > lp" not in f
    for b in ("View the schedule", "Optimize again", ">Publish<"):
        assert b in f


def test_shifts_are_objects_hover_click_double_click_drag():
    s = _src()
    assert "swSelect(+chip.getAttribute('data-swg-chip'))" in s
    assert "document.addEventListener('dblclick'" in s and "swEdit(+chip.getAttribute('data-swg-chip'), chip)" in s
    assert "ssTipShow(c)" in s and "document.addEventListener('dragstart'" in s and "document.addEventListener('drop'" in s
    assert 'draggable="true" class="swg-chip' in s
    drop = s[s.index("function _swDropOk("):]
    drop = drop[:drop.index("\n}\n")]
    assert "role === String(r.role).toLowerCase()" in drop, "a shift only drops on someone in its role"


def test_a_reopened_week_carries_its_economics_and_live_price():
    import inspect
    import mobile_api
    src = inspect.getsource(mobile_api.mobile_schedule_history_detail)
    assert "economics=economics" in src and "projected_cost=projected_cost" in src
    assert "projected_revenue: (d.economics || {}).projected_revenue" in _src()


def test_the_grid_editor_reuses_the_list_editors_rules():
    fin = _fn("schedFinishEdit")
    assert fin.startswith("function schedFinishEdit(i, box)") and "return true;" in fin and fin.count("return false") == 3
    ed = _fn("swEdit")
    assert 'data-sched-f="employee"' in ed and "_fetchReplacements(i" in ed and "_schedFillEmployeeSelect" in ed
    assert "swCloseEdit(_swAddedIdx !== i)" in ed, "opening a new shift's editor must not discard it"


def test_every_edit_redraws_the_grid_and_the_live_panels_in_place():
    assert "if (window.swRenderGrid) swRenderGrid();" in _fn("_schedRenderTable")
    grid = _fn("swRenderGrid")
    assert "_schedPatchChildren(t, h)" in grid and "swLive();" in grid
    assert "_schedPatchTbody(tbody, tHtml)" in _fn("_schedRenderTable")


def test_coverage_names_a_role_scheduled_past_what_its_roster_covers():
    live = _fn("swLive")
    assert "(cnt[rk] || 0) * ceil" in live and "without overtime" in live


def test_the_rules_check_is_split_across_the_right_panel():
    s = _src()
    f = s[s.index("  window.renderScheduleReview=function(d){"):]
    f = f[:f.index("  window.schedMoveOvertime=")]
    for part in ("_swPut(sgEl,hs)", "_swPut(opEl,ho)", "_swPut(fxEl,hf)", "if(!sgEl)h+=hs;if(!opEl)h+=ho;if(!fxEl)h+=hf;"):
        assert part in f


def test_the_advanced_ai_notes_and_target_save_through_the_targets_endpoint():
    s = _src()
    assert "_swSave({labor_target_pct: +t.value}" in s and "_swSave({sched_notes: t.value}" in s
    assert 'id="sw-notes"' in s and 'maxlength="2000"' in s


def test_the_step_pills_hold_still_and_keep_their_done_circles():
    s = _src()
    # Equal outer columns: the pills stay on the page's centre.
    assert "grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);grid-template-rows:60px;" in s
    go = _fn("studioGo")
    assert "var done = {setup: has, summary: has, schedule: has && stage === 'publish'};" in go
    assert "at > gi" not in go, "done no longer depends on the stage on screen"
    assert "pub.style.visibility = stage === 'publish' ? 'hidden' : ''" in go


def test_the_shift_editor_is_a_centred_modal_with_even_fields():
    s = _src()
    ed = _fn("swEdit")
    assert "scrim.id = 'swp-scrim'" in ed and "swp-sp" in ed and 'class="sp"' not in ed
    assert ".swp{position:fixed;z-index:986;left:50%;top:50%;transform:translate(-50%,-50%);" in s
    assert ".swp-g select,.swp-g input{height:44px;" in s
    assert "var sc = document.getElementById('swp-scrim')" in _fn("swCloseEdit")


def test_the_grid_reads_as_rows():
    s = _src()
    # Each row's line is under it (10/2/26), so a group closes with a line and
    # the next role pill sits inside its own group, never above the line.
    assert ".swg-r:not(.swg-hr)>.swg-p,.swg-r:not(.swg-hr)>.swg-c{border-bottom:1px solid var(--hb-line2)}" in s
    assert ".swg-chip~.swg-add{display:none}" in s and ".swg-c .swg-add{position:static;display:flex;flex-direction:column;" in s
    # The add is a ghost chip, ember-outlined, with its words inside it — not a
    # floating + beside a native tooltip (10/2/26); a hovered chip outlines too.
    assert '<span class="t">+ Add a shift</span>' in s and 'title="Add a shift"' not in s
    assert "outline:none;border-color:var(--ember)}" in s
    assert '<div class="swg-role"><span class="pill">' in s
    # A darker shade of the page's own grey (owner, 9/26/26), not an ember tint.
    assert "background:linear-gradient(var(--ss-dayrow,var(--hb-tint2)),var(--ss-dayrow,var(--hb-tint2))),var(--surface)}" in s
    assert '[data-theme="dark"] #studio{--ss-dayrow:rgba(0,0,0,.3);' in s
    assert ".ss-weekhead #sched-gen-meta,.ss-weekhead #sched-gen-notes,.ss-weekhead #sched-verdict{display:none!important}" in s
    assert ".ss #sched-table tbody td{font-size:14.5px!important;" in s


def test_edit_in_the_shift_tab_opens_the_editor():
    # The click that opened the editor bubbled to the outside-click check
    # and closed it again, so Edit looked dead (owner, 9/26/26).
    s = _src()
    assert "_swJustOpened = true; setTimeout(function () { _swJustOpened = false; }, 0);" in _fn("swEdit")
    assert "!t.closest('#swp') && !_swJustOpened) swCloseEdit(true);" in s


def test_warnings_filter_hard_and_soft_over_every_breach():
    s = _src()
    f = s[s.index("  window.renderScheduleReview=function(d){"):]
    f = f[:f.index("  window.schedMoveOvertime=")]
    assert 'data-rv-filter="hard"' in f and 'data-rv-filter="soft"' in f
    assert "if(!!v.hard!==(filt==='hard'))continue;" in f
    assert "(gv.hard?'bad':'warn')" in f, "a hard breach reads red, a soft one amber"
    assert "weren\\u2019t checked yet" in f and "ran out of time" not in s
    import shift_quality
    assert "ran out of time" not in open(shift_quality.__file__, encoding="utf-8").read()


def test_a_coverage_gap_can_be_filled_not_only_ticked():
    s = _src()
    assert "data-rec-fill=" in s
    fill = _fn("ssFillGap")
    assert "_fetchReplacements(idx" in fill and "_swWeekHours(nm).hours + hrs <= _swCeil()" in fill
    assert "action: 'accepted'" in fill and "schedAfterEdit()" in fill


def test_the_what_if_offers_nobody_already_working_that_day_and_says_what_it_did():
    s = _src()
    wi = _fn("_sqWhatIfHtml")
    assert "if (_schedRows[i].date === shift.date) on[" in wi
    assert ". Nothing saved." not in s
    assert "Save to keep it', 'success');" in s


def test_the_right_panel_says_one_thing_once():
    s = _src()
    assert ".ss .sw-right #sched-par-banner{display:none!important}" in s
    assert ".ss .sw-right #sq-confidence .cf-r{display:none}" in s
    assert "rows2 && flags.length ?" in _fn("swLive")
    assert "Priced as last saved. Save to re-price your edits." in _fn("swLive")
    assert "OK TO TRAIN" in s and ">TRAINING<" not in s


def test_a_reopened_week_is_scored_against_todays_inputs_as_it_opens():
    # Owner, 9/26/26: "I applied the RECOMMENDED changes and it dropped 9
    # points" - the stored 83 was measured against the history of the day it
    # was built; the first action re-scored it against today's (the demo had
    # been reseeded), and the gap landed on that action.
    f = _fn("ssRescoreOnOpen")
    assert "_sqLiveScore(_schedRows" in f and "renderShiftQuality(d.quality" in f
    assert "when it was built" in f and "_schedDirty) return;" in f
    assert "ssRescoreOnOpen();" in _fn("studioOnResult")


def test_publish_reads_the_rows_on_screen_and_folds_the_saved_weeks_list():
    s = _src()
    pub = _fn("ssRenderPublish")
    assert "al.hidden = _schedDirty" in pub and "Your edits are saved" not in pub
    assert '<details class="ss-pub-alerts" id="ss-pub-alerts">' in s
    assert ".ss-pub-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}" in s
    assert ".ss-pub-go{display:flex;flex-wrap:wrap;gap:12px;margin-top:26px;justify-content:center}" in s
    assert ".ss-pub-go .cbtn{height:50px;width:240px;" in s


def test_list_edits_in_the_studio_use_the_one_editor_and_a_removal_is_said():
    s = _src()
    assert "if (window._ssOpen && window.swEdit) swEdit(i); else schedEditRow(i);" in s
    assert "Removed ' + (gone.employee" in _fn("_schedRemoveRowNow")


def test_a_read_types_itself_once_a_session_and_never_above_the_viewport():
    tw = _fn("typewriterEffect")
    assert "_twWasSeen(key) || above || still" in tw and "window.scrollBy(0, dh)" in tw


def test_overstaffed_is_the_ember_again():
    # Blue for a day, back to the ember (owner, 9/26/26).
    s = _src()
    assert ".lb2-tint{--tc:var(--ember);" in s and ".sb-lane{--tc:var(--ember)}" in s
    ios = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Features/Labor/StaffingBoardSection.swift"), encoding="utf-8").read()
    assert "case .over: return .cavnarEmber" in ios


def test_a_person_with_no_hours_reads_no_shifts_not_0h():
    """At 12px the number face's round 0 beside an h read as the word "Oh"
    (Will, 10/2/26): a row with no hours this week says so in words."""
    s = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")).read()
    assert "(q.hrs > 0 ? '<span class=\"hb-num\">' + _schedHrs(q.hrs) + 'h</span>' : 'No shifts')" in s


def test_a_double_stacks_from_the_top_so_the_rest_of_the_row_stays_aligned():
    """A two-shift day made its row taller and every one-shift box in that
    row centred against it, skewing the row (Will, 10/2/26). Cells and the
    name column start at the top; the orange day says what it means."""
    s = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")).read()
    assert ".swg-c{padding:6px 4px;min-height:46px;display:flex;flex-direction:column;justify-content:flex-start;" in s
    assert ".swg-p{padding:6px 8px;display:flex;flex-direction:column;justify-content:flex-start;" in s
    assert "The busiest day this week: the most scheduled hours" in s


def test_hovering_a_shift_dims_the_rest_of_the_week():
    s = _src()
    assert ".swg-t:has(.swg-chip:hover) .swg-chip:not(:hover){opacity:.4}" in s


def test_a_stacked_second_shift_is_reachable_and_the_busiest_day_reads_orange():
    """The tooltip sat under the hovered chip, over a double's second shift
    (owner, 10/2/26): it now sits under the whole cell and leaves at once
    for another shift. The busiest day's people and hours are ember too."""
    s = _src()
    assert "top = cc.bottom + 8;" in s and "tip.getAttribute('data-for') !== String(c.getAttribute('data-swg-chip'))" in s
    assert ".swg-d.hot b,.swg-d.hot .dh,.swg-d.hot .dh .hb-num{color:var(--ember)}" in s


def test_the_studio_review_reads_short_and_its_actions_are_primary_buttons_under_their_lines():
    """Owner, 10/2/26: Apply fixes and Improve with Cavnar AI are the brand's
    primary buttons with their line above; the shifts under their bar are one
    line each ("25 / 70"), not sentences; a suggestion's confidence is a pill;
    Fill it sits apart from ✓ / ✕; the two folds' headings sit on the panel's
    edge."""
    s = _src()
    assert '<span class="sr-note">Clears the rule breaks: a legal teammate on each shift that breaks one.</span><button type="button" class="cbtn cbtn-primary"' in s
    assert 'data-sr-improve="1" onclick="improveScheduleWithCavnar(this)"' in s
    assert "' came in at ' + b.score" not in s and '<div class="sq-k">Below the bar</div>' in s
    assert "module: 'schedule', pill: true})" in s
    assert ".sq-rec .acts{display:inline-flex;align-items:center;gap:10px;" in s
    assert ".ss .sw-right .sq-all,.ss .sw-right #sq-why-btn{margin-left:0!important;" in s
