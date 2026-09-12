"""Schedule History redesign — an activity timeline instead of flat rows.

Two halves:
  - models._history_summary_line / get_schedule_history: the one-line
    per-row insight is built ONLY from what the Shift Quality Engine
    (shift_quality.evaluate_schedule) actually found for that week, or
    from the hours themselves when nothing was scored — never an invented
    claim like "no leadership issues" the engine never made.
  - templates/dashboard.html: the row markup/CSS (grid layout, hover,
    selected and focus-visible states, status dot + badge, action icons
    on the real .cbtn system) and the incremental-render pagination that
    keeps hundreds of rows scannable without painting them all at once.
"""
import os
import re

from models import _history_summary_line, get_schedule_history, save_schedule_history

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _fn(name):
    s = _src()
    m = re.search(r"function %s\(" % re.escape(name), s) or \
        re.search(r"window\.%s\s*=\s*function\(" % re.escape(name), s)
    assert m, "function not found: " + name
    i = s.index("{", m.start())
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[m.start():j + 1]
    raise AssertionError("unbalanced braces in " + name)


# ── the summary line is honest, not invented ────────────────────────────────

def test_excellent_week_with_a_strength_reads_clean():
    line, tone = _history_summary_line(
        {"checked": True, "band": "excellent", "weaknesses": [],
         "strengths": ["Every closing shift kept a senior server on the floor."]},
        300, 300, None)
    assert line == "Excellent week · Every closing shift kept a senior server on the floor."
    assert tone == "good"


def test_a_weakness_always_wins_over_a_strength_and_forces_warn_tone():
    line, tone = _history_summary_line(
        {"checked": True, "band": "good", "weaknesses": ["Saturday ran thin on kitchen."],
         "strengths": ["Something nice too."]},
        300, 300, None)
    assert "Saturday ran thin on kitchen." in line
    assert "Something nice" not in line
    assert tone == "warn"


def test_manually_edited_prefixes_the_line_without_losing_the_finding():
    line, tone = _history_summary_line(
        {"checked": True, "band": "fair", "weaknesses": ["Thin on tenure Thursday."]},
        300, 300, "2026-09-10T10:00:00")
    assert line.startswith("Manually edited · ")
    assert "Thin on tenure Thursday." in line
    assert tone == "warn"


def test_unscored_week_falls_back_to_real_hours_not_a_guess():
    under, tone_u = _history_summary_line(None, 288, 300, None)
    assert "under budget" in under and tone_u == "good"
    over, tone_o = _history_summary_line({"checked": False}, 312, 300, None)
    assert "over budget" in over and tone_o == "warn"
    on, tone_on = _history_summary_line(None, 300, 300.2, None)
    assert on == "On budget" and tone_on == "info"


def test_unscored_and_no_hours_says_so_plainly_never_blank():
    line, tone = _history_summary_line(None, 0, 0, None)
    assert line == "Not yet scored"
    assert tone == "info"


def test_never_fabricates_a_dimension_the_engine_did_not_report():
    # No weaknesses/strengths at all (engine ran but found nothing notable
    # enough to hoist) — the line must not claim something specific like
    # "no leadership issues" that was never actually checked here.
    line, _ = _history_summary_line({"checked": True, "band": "good"}, 300, 300, None)
    assert "leadership" not in line.lower()
    assert line == "Solid week"


def test_long_findings_are_truncated_not_left_to_overflow_the_row():
    # The row wraps to a second line now (templates/dashboard.html's
    # .lb2-hist .rline .rtxt), so this only needs to guard against a
    # genuinely pathological string — real engine sentences (110-160
    # chars routinely) are meant to display in full, not clip mid-word.
    line, _ = _history_summary_line(
        {"checked": True, "band": "weak", "weaknesses": ["x" * 400]}, 300, 300, None)
    assert len(line) <= 221
    assert line.endswith("…")


def test_a_realistic_long_finding_is_not_clipped():
    line, _ = _history_summary_line(
        {"checked": True, "band": "weak",
         "weaknesses": ["Only 0 of 6 have worked 20+ shifts here; this shift usually "
                        "wants about 30%. Similar on every shift this week."]},
        300, 300, None)
    assert line.endswith("this week.")
    assert "…" not in line


def test_get_schedule_history_carries_the_summary_and_edited_by(tmp_path):
    db = str(tmp_path / "t.db")
    save_schedule_history(1, "2026-09-07", "2026-09-13", 300, 300, 26.0,
                          "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n",
                          [], quality={"checked": True, "band": "excellent", "score": 94,
                                       "confidence": {"level": "high"}, "strengths": [], "weaknesses": []},
                          db_path=db)
    rows = get_schedule_history(1, db_path=db)
    assert len(rows) == 1
    r = rows[0]
    assert r["summary_tone"] == "good"
    assert r["summary_line"] == "Excellent week"
    assert r["quality_score"] == 94
    assert "edited_by" in r  # column now selected, even though None here


# ── the row markup: grid, states, real buttons, honest badges ──────────────

def test_row_is_a_grid_with_hover_selected_and_focus_visible_states():
    css = _src()
    m = re.search(r"\.lb2-hist \.row\{([^}]*)\}", css)
    assert m and "grid" in m.group(1)
    assert ".lb2-hist .row:hover{" in css
    assert ".lb2-hist .row.selected{" in css
    assert ".lb2-hist .row:focus-visible{" in css


def test_row_is_keyboard_operable_and_carries_an_accessible_name():
    body = _fn("_schedHistRowHtml")
    assert 'tabindex="0"' in body and 'role="button"' in body
    assert "aria-label=" in body
    assert "onkeydown=" in body and "Enter" in body


def test_action_icons_are_real_cbtn_buttons_not_a_bespoke_style():
    body = _fn("_schedHistRowHtml")
    assert "cbtn cbtn-icon cbtn-text cbtn-muted cbtn-sm ric-view" in body
    assert "cbtn cbtn-icon cbtn-text cbtn-danger cbtn-sm" in body
    # Every inline <svg> icon carries explicit pixel dimensions — an <svg>
    # with only a viewBox defaults to 300x150 and would blow out a 30px button.
    for svg in re.findall(r"<svg[^>]*>", body):
        assert 'width="15"' in svg and 'height="15"' in svg


def test_action_buttons_stop_propagation_so_they_dont_also_open_the_row():
    body = _fn("_schedHistRowHtml")
    for fn in ("viewScheduleHistory", "downloadHistoryCsv", "deleteScheduleHistory"):
        assert ("event.stopPropagation();" + fn) in body


def test_badge_reflects_real_edited_state_not_a_fabricated_status():
    body = _fn("_schedHistRowHtml")
    assert '"badge edited"' in body
    # A generated-by-AI badge used to render on every unedited row — removed
    # per feedback that it added no information (every row not marked
    # "Edited" is AI-generated by definition) and just added visual noise.
    assert '"badge ai"' not in body
    # No Draft/Published/Archived — those states aren't tracked anywhere
    # for a history row, so the badge must not claim them.
    for fake in ("Draft", "Published", "Archived"):
        assert fake not in body


def test_quality_pill_tone_follows_the_engines_own_band():
    body = _fn("_schedHistRowHtml")
    assert "band==='excellent'||band==='good'" in body.replace(" ", "")


# ── incremental rendering keeps hundreds of rows cheap to scan ─────────────

def test_history_renders_in_pages_not_all_at_once():
    s = _src()
    assert "SCHED_HIST_PAGE" in s
    body = _fn("_schedHistRenderMore")
    assert "_schedHistAll.slice(_schedHistShown,next)" in body
    assert "cavnarWhenVisible(document.getElementById('sched-history-more')" in body


def test_loading_state_is_a_skeleton_not_a_loading_string():
    body = _fn("loadScheduleHistory")
    assert "_schedHistSkeletonHtml" in body
    assert "Loading…'" not in body and 'Loading...' not in body


def test_empty_state_is_wrapped_in_the_shared_empty_component():
    body = _fn("loadScheduleHistory")
    assert 'class="hb-empty"' in body
    assert "No schedules generated yet" in body


def test_selected_row_tracking_survives_close_and_delete():
    s = _src()
    assert "window.closeScheduleHistoryDetail=function(){" in s
    assert "window._schedHistOpenId=null" in s
    # Deleting the currently-open row must also clear the selection/detail,
    # not leave a highlighted row pointing at content that no longer exists.
    body = _fn("deleteScheduleHistory")
    assert "closeScheduleHistoryDetail()" in body
