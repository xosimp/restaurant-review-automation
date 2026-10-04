"""Schedule fix round 10/3/26, UI wave (iOS workstream I2): each new iOS
element reads the server's own key and calls a route the server serves —
the team and person sheet, the rules sheet, the closers and roles sheets,
the labor standards and next week's forecast, the PAR check, the learned
patterns, calibration, the coverage card on Home, Account → Memory, and the
staff app's time off and attendance. Source-level, like test_mem_ui_ib.py:
an element whose key or route drifts from the server's fails here.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _swift(rel):
    with open(os.path.join(APP, rel), encoding="utf-8") as fh:
        return fh.read()


def _py(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


DECODED_KEYS = {
    "Features/Labor/ScheduleSetupViewModel.swift": [
        # roster: the owner's per-person facts (F1), dormancy, roles, ratings (F2)
        "floor_manager", "paid_hourly", "acting_manager", "standing_shifts", "closes_for",
        "can_close_pending", "last_worked_label", "dormant_text", "recent_roles", "role_scores",
        "rating_due_text", "certification_labels", "can_edit_owner_facts",
        # reliability (G)
        "called_out", "late_shifts", "late_rate", "late_risk",
        # rules patch
        "role_close_mins", "salaried_cap",
        # learned patterns (H1)
        "dismissed_by_admin", "admin_saves", "admin_dismissals", "can_adopt",
        # calibration (D1b) and the record from punches (F2)
        "moving_profiles", "left_out", "from_punches",
        # the booking export's report (E)
        "skipped_status", "skipped_unreadable", "late_covers",
    ],
    "Features/Labor/TeamSetupModels.swift": [
        "target_role", "not_counted", "ask_standing", "missing_standing", "by_role", "closer_roles_basis",
        "closer_roles_in_force", "pending_admin", "outside_roles", "reads_as", "may_run", "per_hour",
        "budget_basis", "daily_target_reasons", "demand_data_through", "salaried_week_cost", "target_dollars",
        "salaries_exceed_target", "trim_ok", "labor_budget_dollars",
    ],
    "Features/Labor/TeamSetupStore.swift": [
        "managers_line", "owner_rules", "owner_rules_unchecked", "close_times_missing", "role_close_conflicts",
        "salaried_cap_default", "salaried_cap_bounds", "floor_cap_conflicts", "cross_training_defaults",
        "closer_roles", "salaried_remove", "salaried_add", "can_close",
    ],
    "Features/Labor/LaborViewModel.swift": [
        "rated_label", "rating_due", "due_for_rerate", "admin_set", "leader_rule_defaults",
        "leader_rules_status", "leader_rule_warnings", "hours_hourly", "hours_salaried", "budget_basis",
    ],
    "Features/Labor/TimeOffSection.swift": ["span_label"],
    "Features/Staff/StaffRequestModels.swift": ["span_label"],
    "Features/Staff/StaffRequestsViews.swift": ["start_time", "end_time", "lunch", "dinner"],
    "Features/Staff/StaffModels.swift": ["shifts_checked", "minutes_late", "date_label"],
    "Features/Home/HomeDay.swift": ["covered_by", "shift_start", '"for"'],
    "Features/Account/AccountMemoryView.swift": ["schedule_reads", "schedule_unchecked", "schedule_rule"],
    "Models/MemoryRecall.swift": ["last_hand", "retest_since", "retired_reason"],
}


@pytest.mark.parametrize("rel", sorted(DECODED_KEYS))
def test_each_decoder_reads_the_servers_key(rel):
    src = _swift(rel)
    missing = [k for k in DECODED_KEYS[rel] if (k if k.startswith('"') else f'"{k}"') not in src]
    assert not missing, f"{rel} no longer decodes {missing}"


ROUTES = {
    "Features/Labor/TeamSetupStore.swift": [
        "/mobile/api/labor/managers", "/mobile/api/labor/closers", "/mobile/api/labor/role-families",
        "/mobile/api/labor/floors/suggest", "/mobile/api/labor/labor-standards",
        "/mobile/api/labor/schedule-forecast", "/mobile/api/account/targets",
        "/mobile/api/labor/team/ratings/adopt",
    ],
    "Features/Labor/ScheduleSetupViewModel.swift": [
        "/mobile/api/labor/staff-settings", "/mobile/api/labor/rules", "/mobile/api/labor/learned-patterns",
        "/mobile/api/labor/learned-patterns/adopt", "/mobile/api/labor/schedule/adopt-admin-saves",
        "/mobile/api/labor/demand-signals",
    ],
    "Features/Labor/LaborViewModel.swift": ["/mobile/api/labor/team/rating", "/mobile/api/labor/team/thresholds"],
    "Features/Home/HomeDay.swift": ["/ask-cover"],
    "Features/Staff/StaffRequestsViews.swift": ["/staff/api/time-off"],
    "Features/Staff/StaffPortalStore.swift": ["/staff/api/stats"],
    "Features/Account/AccountMemoryView.swift": ["/mobile/api/account/memory/add"],
}

SERVER_ROUTES = [
    ("strategy_routes.py", '"/labor/managers"'),
    ("strategy_routes.py", '"/labor/closers"'),
    ("strategy_routes.py", '"/labor/role-families"'),
    ("strategy_routes.py", '"/labor/floors/suggest"'),
    ("strategy_routes.py", '"/labor/labor-standards"'),
    ("strategy_routes.py", '"/labor/schedule-forecast"'),
    ("strategy_routes.py", '"/account/targets"'),
    ("strategy_routes.py", '"/labor/staff-settings"'),
    ("strategy_routes.py", '"/labor/rules"'),
    ("strategy_routes.py", '"/labor/learned-patterns/adopt"'),
    ("strategy_routes.py", '"/labor/schedule/adopt-admin-saves"'),
    ("strategy_routes.py", '"/issues/<int:issue_id>/ask-cover"'),
    ("strategy_routes.py", '"/account/memory/add"'),
    ("mobile_api.py", '@mobile_bp.route("/labor/team/ratings/adopt"'),
    ("mobile_api.py", '@mobile_bp.route("/labor/team/rating"'),
    ("staff_routes.py", '@staff_bp.route("/api/time-off", methods=["POST"])'),
]


@pytest.mark.parametrize("rel", sorted(ROUTES))
def test_the_phone_calls_each_route(rel):
    src = _swift(rel)
    missing = [p for p in ROUTES[rel] if p not in src]
    assert not missing, f"{rel} no longer calls {missing}"


@pytest.mark.parametrize("module,decl", SERVER_ROUTES)
def test_each_route_the_phone_calls_is_served(module, decl):
    assert decl in _py(module), f"{module} no longer declares {decl}"


def test_the_floor_manager_choice_is_sent_as_the_route_reads_it():
    vm = _swift("Features/Labor/ScheduleSetupViewModel.swift")
    assert 'try c.encode("auto", forKey: .floorManager)' in vm
    store = _swift("Features/Labor/TeamSetupStore.swift")
    assert 'floorManager = "floor_manager"' in store
    import staff_settings
    assert staff_settings._clean_floor_manager("auto") == staff_settings.AUTO


def test_owner_only_controls_follow_the_servers_flag():
    sheet = _swift("Features/Labor/RosterSection.swift")
    assert "canEdit: viewModel.canEditOwnerFacts" in sheet
    rules = _swift("Features/Labor/ScheduleRulesSheet.swift")
    # the salaried list and the staffing rules' words are the owner's alone
    assert re.search(r"if viewModel\.canEditRules \{\s*RulesSalariedSection", rules)


def test_the_par_check_holds_hourly_hours_against_the_hourly_budget():
    par = _swift("Features/Labor/ParHoursCheck.swift")
    assert "hourly ?? scheduled" in par and "Hourly budget" in par
    for rel in ("Features/Labor/LaborView.swift", "Features/ScheduleHistory/ScheduleHistoryDetailView.swift"):
        src = _swift(rel)
        assert "ParHoursCheck(" in src and "Budgeted \\(" not in src


def test_a_blank_shortest_shift_is_sent_as_no_rule():
    rules = _swift("Features/Labor/ScheduleRulesSheet.swift")
    assert '"min_shift_hours"' in rules and '"full_time_min_hours"' in rules
    assert 'out["min_shift_hours"] = .null' in rules


def test_excused_reads_drop_approved_in_the_staff_app():
    assert '"Drop approved"' in _swift("Features/Staff/StaffModels.swift")
    import staff_insights
    assert staff_insights.OUTCOME_LABELS["excused"] == "Drop approved"


def test_no_owner_facing_date_formatter_or_bare_brand_in_new_files():
    for rel in ("Features/Labor/TeamSetupModels.swift", "Features/Labor/TeamSetupStore.swift",
                "Features/Labor/PersonScheduleFacts.swift", "Features/Labor/CloserCleanupSheet.swift",
                "Features/Labor/RoleFamiliesSheet.swift", "Features/Labor/ScheduleRulesSetupSections.swift",
                "Features/Labor/LaborStandardsSection.swift", "Features/Labor/ForecastPreviewSection.swift",
                "Features/Labor/ParHoursCheck.swift", "Features/Staff/StaffAttendanceSection.swift",
                "DesignSystem/CavnarDateTimeChips.swift"):
        src = _swift(rel)
        assert "MMM" not in src and "h:mm a" not in src, rel
        for lit in re.findall(r'"((?:[^"\\]|\\.)*)"', src):
            assert not re.search(r"\bCavnar\b(?! AI)", lit), f"bare 'Cavnar' in {rel}: {lit!r}"
