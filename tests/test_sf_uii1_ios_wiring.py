"""Schedule fix round 10/3/26, UI wave (iOS workstream I1): the Labor
draft, generate, review, publish check, Shift Quality and learning screens
read the server's own keys and call routes the server serves. Source-level,
like test_sf_uii2_ios_wiring.py: an element whose key or route drifts from
the server's fails here, and each server key is checked to still be sent.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")
LABOR = "Features/Labor/"


def _swift(rel):
    with open(os.path.join(APP, rel), encoding="utf-8") as fh:
        return fh.read()


def _py(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# Each Swift file → the JSON keys it must decode (as a CodingKeys raw value).
DECODED = {
    LABOR + "ScheduleFixModels.swift": [
        # the manager plan (M) and the backstop (A2)
        "unknown_pattern", "could_act", "short_by",
        # the review's new parts (B1, C2, F1, F2, A1)
        "blocks_publish", "can_adopt", "shift_start",
        # publish check (G, H1)
        "likelihood",
        # sections (H2), memory (H2), measured ratings (H2)
        "foh_roles", "class_label", "status_label", "held_in_code", "bound_by", "confidence_pct",
        "last_confirmed_by_hand", "retired_words", "can_keep", "can_let_go", "can_be_rule",
        "consolidated_at", "can_answer", "can_make_rules", "min_tickets",
        # shift quality (D1a)
        "no_history",
    ],
    LABOR + "LaborViewModel.swift": [
        # rows: the plan's pin, the stable id, the clock change, Cavnar AI's flag
        "_pinned", "_pin_reason", "_rid", "dst_hours", "origin_sig",
        # review extras
        "hard_days", "stage_failures", "unmatched_names", "budget_conflict", "cap_floor_conflicts",
        "manager_plan", "manager_coverage", "min_hours", "unwritten_dates", "unstaffable_dates",
        # generation: partial week, starting point, requirements, polling
        "starting_point", "seconds_left", "wait_seconds",
        # save and the why; apply fixes / Improve removals
        "why_questions", "cavnar_changes", "cavnar_removed",
        # generate body
        "reason_chip", "reason_text", "instruction",
        # quality (D1a, D1b, D2)
        "held_by", "no_shift_written", "top_reason", "on_demand", "checked_with",
        "dollars_before", "dollars_after", "what_if",
        # freshness (E)
        "blocked",
    ],
    LABOR + "PublishScheduleSheet.swift": ["notes", "hours", "likely_to_change"],
    LABOR + "ScheduleSetupViewModel.swift": ["avg_actual_hours", "actual_weeks", "stayed_late", "labor_pct",
                                             "splh_basis"],
}


@pytest.mark.parametrize("rel,keys", sorted(DECODED.items()))
def test_each_new_ios_element_decodes_the_servers_key(rel, keys):
    src = _swift(rel)

    def reads(k):
        # A CodingKeys raw value ("hard_days"), or a case named as the key.
        return f'"{k}"' in src or re.search(r"\bcase\b[^\n=]*\b" + re.escape(k) + r"\b", src) is not None
    missing = [k for k in keys if not reads(k)]
    assert not missing, f"{rel} does not read {missing}"


ROUTES = {
    "/mobile/api/labor/schedule/edit-why": ("strategy_routes.py", '"/labor/schedule/edit-why"'),
    "/mobile/api/labor/schedule-memory": ("strategy_routes.py", '"/labor/schedule-memory"'),
    "/mobile/api/labor/ratings/suggested": ("strategy_routes.py", '"/labor/ratings/suggested"'),
    "/mobile/api/labor/schedule/sections": ("mobile_api.py", '"/labor/schedule/sections"'),
    "/mobile/api/labor/staff-settings": ("strategy_routes.py", '"/labor/staff-settings"'),
    "/mobile/api/labor/roster": ("strategy_routes.py", '"/labor/roster"'),
    "/mobile/api/labor/team/ratings/adopt": ("mobile_api.py", '"/labor/team/ratings/adopt"'),
    "/mobile/api/labor/schedule-forecast": ("strategy_routes.py", '"/labor/schedule-forecast"'),
    "/mobile/api/labor/schedule/score": ("mobile_api.py", '"/labor/schedule/score"'),
}


@pytest.mark.parametrize("path,server", sorted(ROUTES.items()))
def test_each_route_the_phone_calls_is_served(path, server):
    swift = "".join(_swift(LABOR + f) for f in ("LaborViewModel.swift", "ScheduleMemoryScreen.swift"))
    assert f'"{path}"' in swift, f"the phone no longer calls {path}"
    py, needle = server
    assert needle in _py(py), f"{py} no longer serves {needle}"


def test_the_server_still_sends_what_the_phone_reads():
    """The payload keys the new elements read, on the server side."""
    engine = _py("schedule_engine.py")
    for key in ("manager_plan=", "manager_coverage=", "min_hours=", "unwritten_dates=", "unstaffable_dates=",
                "starting_point=", "requirements=", "hours_hourly=", "calls="):
        assert key in engine, key
    for key in ('"hard_days"', '"hard_rows"'):
        assert key in _py("schedule_rules.py"), key
    mobile = _py("mobile_api.py")
    assert "why_questions=" in mobile and "wait_seconds=" in mobile and "seconds_left=" in mobile
    routes = _py("strategy_routes.py")
    assert '"cavnar_removed": gone' in routes and '"likely_to_change": likely' in routes
    assert '"top_reason"' in _py("shift_quality.py") and '"held_by"' in _py("shift_quality.py")
    assert '"checked_with"' in _py("shift_quality.py")
    assert '"dollars_before"' in _py("schedule_optimizer.py")


def test_the_redo_chips_are_the_servers_reasons():
    """The sheet sends REDO_REASONS keys; every key it sends is one the
    server knows (a chip off the list would be kept only as a label)."""
    src = _swift(LABOR + "ScheduleFixViews.swift")
    sent = re.findall(r'\("([a-z_]+)", "[^"]+"\)', src)
    engine = _py("schedule_engine.py")
    block = engine[engine.index("REDO_REASONS = {"):engine.index("}", engine.index("REDO_REASONS = {"))]
    assert sent and all(f'"{k}"' in block for k in sent), sent


def test_save_sends_cavnar_changes_and_clears_them():
    vm = _swift(LABOR + "LaborViewModel.swift")
    assert "cavnarChanges: save && !cavnarRemoved.isEmpty ? cavnarRemoved : nil" in vm
    assert "cavnarRemoved = []" in vm
    # Rows keep Cavnar AI's flag both ways (decoded and sent back).
    assert 'case originSig = "origin_sig"' in vm


def test_poll_waits_the_jobs_own_time_not_a_fixed_count():
    vm = _swift(LABOR + "LaborViewModel.swift")
    assert "for attempt in 0..<450" not in vm
    assert "result.secondsLeft" in vm and "waitSeconds: response.waitSeconds" in vm


def test_a_day_level_breach_shows_on_the_day_not_a_row():
    views = _swift(LABOR + "ScheduleFixViews.swift")
    assert "hardDays?.items" in views
    labor = _swift(LABOR + "LaborView.swift")
    assert "DayManagerNotes(" in labor and "ScheduleRowBadges(" in labor


def test_the_labor_focus_opens_the_action_queues_new_items():
    labor = _swift(LABOR + "LaborView.swift")
    assert 'case "ratings": self = .ratings' in labor and 'case "intel": self = .intel' in labor
    queue = _py("action_queue.py")
    assert '"nav": "labor/ratings"' in queue and '"nav": "labor/intel"' in queue


def test_the_change_history_names_a_calibration_suggestion():
    assert '"quality_profiles_suggested"' in _swift("Models/MemoryRecall.swift")
    assert '"quality_profiles_suggested"' in _py("strategy_jobs.py")


def test_new_patterns_are_documented():
    ds = _py("DESIGN_SYSTEM.md")
    for name in ("ScheduleNotice", "ScheduleRowTag", "DayManagerNotes", "ManagerQuestionCard", "RedoDaysSheet",
                 "EditWhySheet", "Look for a better arrangement", "ScheduleMemoryScreen"):
        assert name in ds, name
