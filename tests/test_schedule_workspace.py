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
    left = ws[ws.index('id="ss-settings"'):ws.index('class="sw-center"')]
    right = ws[ws.index('id="sw-right"'):]
    # Settings as tabs, not every setting at once (owner, 9/26/26).
    assert re.findall(r'data-ss-tab="(\w+)"', left) == ["basic", "forecast", "employees", "rules", "ai", "advanced"]
    assert re.findall(r'data-ss-panel="(\w+)"', left) == ["basic", "forecast", "employees", "rules", "ai", "advanced"]
    assert 'id="gen-sched-btn"' in left[left.index('data-ss-panel="advanced"'):], "Generate closes the settings"
    assert _kickers(right) == ["Shift quality", "Coverage", "Labor", "Cost", "Warnings", "Opportunities",
                               "AI suggestions", "Apply fixes"]
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
    assert stages == ["setup", "build", "summary", "schedule", "publish", "history"]
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
    assert "Projection · not yet earned" in f and "rec.pct > lp" in f
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
