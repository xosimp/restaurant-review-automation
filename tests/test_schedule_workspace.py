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
    ws = s[s.index('id="sched-ws"'):s.index('<div class="lb2-sched-rest">')]
    left = ws[ws.index('class="sw-side sw-left"'):ws.index('class="sw-center"')]
    right = ws[ws.index('id="sw-right"'):]
    assert _kickers(left) == ["Generation settings", "Forecast", "Labor target", "Employees", "Constraints", "Advanced AI"]
    assert 'id="gen-sched-btn"' in left[left.index("Advanced AI"):], "Generate closes the left panel"
    assert _kickers(right) == ["Shift quality", "AI suggestions", "Warnings", "Coverage", "Labor", "Cost",
                               "Opportunities", "Apply fixes"]
    center = ws[ws.index('class="sw-center"'):ws.index('id="sw-right"')]
    for part in ('id="schedule-preview-panel"', 'id="swg"', 'id="sched-table-wrap"', 'data-sw-view="week"',
                 'data-sw-view="list"', 'id="sched-reopen"'):
        assert part in center


def test_the_workspace_breaks_out_of_the_column_and_stacks_when_narrow():
    s = _src()
    assert ".sw{--sw-w:min(1480px,calc(100vw - 48px));width:var(--sw-w);margin-left:calc((100% - var(--sw-w)) / 2)" in s
    assert "@media (max-width:1320px){.sw-grid{grid-template-columns:256px minmax(0,1fr)}" in s
    assert "@media (max-width:900px){.sw-grid{grid-template-columns:1fr}" in s
    assert ".lb2-sched.sw{padding:0;background:none;border:0" in s, "three panels, not one card"


def test_the_building_animation_is_kept_and_fills_the_week_column():
    body = _src()[_src().index("function generateSchedule("):]
    body = body[:body.index("fetch('/api/generate-schedule'")]
    assert "cavnarWeekHtml(_wkLabel)" in body
    assert "_swCtr.insertBefore(_wk, _swCtr.firstChild)" in body


def test_the_grid_editor_reuses_the_list_editors_rules():
    fin = _fn("schedFinishEdit")
    assert fin.startswith("function schedFinishEdit(i, box)") and "return true;" in fin and fin.count("return false") == 3
    ed = _fn("swEdit")
    assert 'data-sched-f="employee"' in ed and "_fetchReplacements(i" in ed and "_schedFillEmployeeSelect" in ed
    assert "swCloseEdit(_swAddedIdx !== i)" in ed, "opening a new shift's editor must not discard it"


def test_every_edit_redraws_the_grid_and_the_live_panels_in_place():
    s = _src()
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
