"""iOS Labor blind re-audit (10/8/26): source-level guards for the rules the
fix round protects — the PAR target is a ceiling, a queued send is never
read as sent, the manager gaps are said at the week's level, outward and
destructive taps are asked or undoable, and the web sections the phone
links to exist."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABOR = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Labor")


def _src(name):
    with open(os.path.join(LABOR, name), encoding="utf-8") as f:
        return f.read()


def test_par_hours_any_hour_over_the_budget_is_over():
    s = _src("ParHoursCheck.swift")
    # No tolerance band: 5% over used to read a green "On budget" (H2).
    assert "budget * 0.05" not in s
    assert "if diff > 0 { return (" in s


def test_the_scorecard_labor_tile_is_over_at_any_amount_above_target():
    s = _src("ScheduleWeekViews.swift")
    assert "pct > $0 + 0.5" not in s


def test_a_queued_publish_is_decoded_and_never_read_as_sent():
    s = _src("PublishScheduleSheet.swift")
    for key in ('case actionId = "action_id"', 'case undoMinutes = "undo_minutes"', "case ok, sent, unreachable, failed, error, status, acknowledged, note, version, queued"):
        assert key in s
    assert "var hasSent: Bool { lastResult?.ok == true && lastResult?.queued != true }" in s
    assert "client.undoQueuedAction(id)" in s
    # Who opened it is read for the week being sent (M2).
    assert '"/mobile/api/labor/schedule-share-status", query: query' in s


def test_manager_gaps_are_said_at_the_weeks_level():
    models = _src("ScheduleFixModels.swift")
    assert "var managerGaps: [ManagerGap]" in models and "var attentionParts: [String]" in models
    view = _src("LaborView.swift")
    assert "result.attentionExtraCount" in view          # the send bar's Review N
    assert "result.managerGaps" in view                   # the week-level notice
    assert "managerGap: date.map" in view                 # the day chip's red dot
    assert "page.managerGap" in _src("ScheduleWeekViews.swift")


def test_outward_and_destructive_taps_are_asked_or_undoable():
    wait = _src("LaborWaitingOnYou.swift")
    assert "confirmingDeny = PendingDeny(" in wait and "Task { await deny() }" not in wait
    panel = _src("ScheduleReviewPanel.swift")
    assert "confirmingStandby = day" in panel and "undoOvertimeMove()" in panel
    assert "confirmingActing = name" in _src("ScheduleFixViews.swift")
    assert "removing = entry" in _src("AvailabilityManagerSection.swift")
    memory = _src("TeamMemorySection.swift")
    assert 'answerWithUndo(note, part: part, action: "remove")' in memory
    assert 'answerWithUndo(note, part: part, action: "expire")' in memory
    # Leaving inside the Undo window keeps the note (the safe side).
    assert ".onDisappear { viewModel.undoPendingNote() }" in memory
    assert "confirmingSame = q" in memory


def test_the_restaurant_wide_target_is_not_stepped_from_the_generate_sheet():
    s = _src("ScheduleBuildSettings.swift")
    body = s[s.index("private var targetRow: some View"):s.index("private func stepButton")]
    assert "stepButton(" not in body and 'path: "account/restaurant"' in body


def test_the_cover_picker_reads_who_the_approve_would_take():
    assert "/candidates" in _src("ScheduleSetupViewModel.swift")
    assert "loadCoverCandidates(requestId:" in _src("ShiftRequestsSection.swift")


def test_the_web_sections_the_phone_links_to_exist():
    with open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8") as f:
        page = f.read()
    for sec in ("labor/rules", "labor/events", "labor/intel", "labor/closers", "labor/tasks", "account/restaurant"):
        assert 'data-nav="%s"' % sec in page, sec
    paths = set()
    for name in os.listdir(LABOR):
        if name.endswith(".swift"):
            paths |= set(re.findall(r'path: "(labor/[a-z]+)"', _src(name)))
    for p in paths:
        assert 'data-nav="%s"' % p in page or p in ("labor/overtime",), p
