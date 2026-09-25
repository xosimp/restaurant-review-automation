"""Friction audit (9/25/26), workstream I2 — the iOS command sheet, the ways
in from outside the app, and the module-screen fixes (TOP50 #47, #31, #28,
#18, #19, #25, #41, #49).

These read the Swift sources and project.yml: the suite cannot run the iOS
app, and each rule below must hold in the source whatever fixture a screen
happens to be rendered with. The pure logic has XCTest cover too
(CavnarAITests/CommandEntryPointsTests.swift).
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI")
APP = os.path.join(IOS, "CavnarAI")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def project():
    """project.yml split into its targets' blocks (no YAML parser in the
    test environment; xcodegen is the real reader)."""
    text = _read(IOS, "project.yml")
    targets = text.split("\ntargets:\n", 1)[1].split("\nschemes:\n", 1)[0]
    blocks = {}
    for chunk in re.split(r"\n(?=  [A-Za-z][\w ()]*:\n)", "\n" + targets):
        head = chunk.strip().split(":", 1)[0]
        if head:
            blocks[head] = chunk
    return blocks


# ── #31 / #47: system entry points ───────────────────────────────────────────

def test_static_quick_actions_match_the_code_that_handles_them(project):
    types = set(re.findall(r"UIApplicationShortcutItemType: ([\w.-]+)", project["CavnarAI"]))
    entry = _read(APP, "Core", "SystemEntry.swift")
    handled = set(re.findall(r'case \w+ = "(ai\.cavnar\.quick\.[\w-]+)"', entry))
    assert types == {"ai.cavnar.quick.ask", "ai.cavnar.quick.last-night", "ai.cavnar.quick.scan-invoice"}
    assert types <= handled, "every static quick action needs a QuickAction case"
    # The approve item is runtime-only, with its count.
    assert "ai.cavnar.quick.approve-replies" in handled
    assert "approveRepliesItem(waiting:" in _read(APP, "Core", "SystemSync.swift")


def test_the_widget_extension_is_a_real_target_embedded_in_the_app(project):
    widgets = project["CavnarWidgets"]
    app = project["CavnarAI"]
    assert "type: app-extension" in widgets
    assert "NSExtensionPointIdentifier: com.apple.widgetkit-extension" in widgets
    assert re.search(r"dependencies:\n\s+- target: CavnarWidgets", app)
    group = "group.ai.cavnar.CavnarAI"
    for block in (widgets, app):
        assert re.search(r"com\.apple\.security\.application-groups:\n\s+- " + re.escape(group), block)
    assert f'"{group}"' in _read(IOS, "Shared", "WidgetSnapshot.swift")
    assert re.search(r'SWIFT_ACTIVE_COMPILATION_CONDITIONS: ".*CAVNAR_WIDGET_EXTENSION"', widgets)
    # Both targets compile the shared snapshot + activity attributes.
    assert "- path: Shared" in widgets and "- path: Shared" in app


def test_the_app_declares_live_activities_and_the_camera(project):
    app = project["CavnarAI"]
    assert "NSSupportsLiveActivities: true" in app
    assert re.search(r'NSCameraUsageDescription: ".*invoice', app)


def test_the_widget_extension_never_signs_in_or_calls_the_api():
    """A second target holding a token is a second session to secure. The
    extension draws what the app wrote; the Undo intent's network call is
    compiled out of it and runs in the app's process."""
    for name in os.listdir(os.path.join(IOS, "CavnarWidgets")):
        if name.endswith(".swift"):
            src = _read(IOS, "CavnarWidgets", name)
            for banned in ("APIClient", "Keychain", "sendWithBearer", "URLSession"):
                assert banned not in src, f"CavnarWidgets/{name} uses {banned}"
    shared = _read(IOS, "Shared", "PendingSendActivity.swift")
    block = shared.split("#if CAVNAR_WIDGET_EXTENSION", 1)[1].split("#endif", 1)[0]
    assert "#else" in block and "PendingSendCanceller" in block.split("#else", 1)[1]
    assert "PendingSendCanceller" not in shared.split("#if CAVNAR_WIDGET_EXTENSION", 1)[0]
    assert "authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication" in shared


def test_signing_out_clears_what_the_lock_screen_shows():
    sync = _read(APP, "Core", "SystemSync.swift")
    signed_out = sync.split("guard let token = Keychain.get(Keychain.Key.sessionToken)", 1)[1].split("return\n", 1)[0]
    # One helper, called here and by SessionStore at sign-out itself (F3-8).
    assert "Self.clearForSignOut()" in signed_out
    signed_out = sync.split("static func clearForSignOut() {", 1)[1].split("\n    }\n", 1)[0]
    assert "WidgetSnapshot.clear()" in signed_out
    assert "PendingSendActivities.endAll()" in signed_out
    assert "shortcutItems = []" in signed_out


def test_app_shortcuts_only_open_the_app_except_undo():
    """Siri must never send, post or approve: every intent but Undo opens
    the app, and anything outward still meets the in-app confirm card."""
    src = _read(APP, "Core", "CavnarAppIntents.swift")
    intents = re.split(r"\nstruct ", src)
    for chunk in intents:
        name = chunk.split(":", 1)[0].strip()
        if not name.endswith("Intent"):
            continue
        if name == "UndoSoonestPendingSendIntent":
            assert "openAppWhenRun: Bool = false" in chunk
            assert ".requiresAuthentication" in chunk
            continue
        assert "openAppWhenRun: Bool = true" in chunk, name
        assert "client.send" not in chunk and "APIClient" not in chunk, name
    assert "AppShortcutsProvider" in src


def test_links_from_outside_route_through_one_door():
    app = _read(APP, "CavnarAIApp.swift")
    assert ".onOpenURL { url in SystemEntry.handle(url: url) }" in app
    assert "NSUserActivityTypeBrowsingWeb" in app
    entry = _read(APP, "Core", "SystemEntry.swift")
    # The router owns navigation: the entry points only post the nav path.
    assert "NotificationCenter.default.post(name: .cavnarOpenNav, object: path)" in entry
    assert "pendingTab" not in entry and "pendingModuleKey" not in entry


# ── #47: the command sheet ───────────────────────────────────────────────────

def test_the_command_sheet_reads_the_shared_command_routes():
    vm = _read(APP, "Features", "Command", "CommandSheetViewModel.swift")
    for route in ("/mobile/api/command/registry", "/mobile/api/command/search",
                  "/mobile/api/command/propose", "/mobile/api/actions", "/mobile/api/actions/pending"):
        assert f'"{route}"' in vm, route
    # The confirm is Ask's own card and Ask's own confirm path — no second
    # way to run an action.
    assert "AskCavnarViewModel()" in vm
    sheet = _read(APP, "Features", "Command", "CommandSheet.swift")
    assert "ProposalCard(proposal: proposal, viewModel: viewModel.askViewModel)" in sheet
    # Return in the field opens or asks; it never confirms or proposes.
    submit = sheet.split("private func submit() {", 1)[1].split("\n    }\n", 1)[0]
    assert "confirm" not in submit and "propose" not in submit


def test_the_command_sheet_never_edits_the_router():
    for name in ("CommandSheet.swift", "CommandSheetViewModel.swift", "CommandModels.swift"):
        src = _read(APP, "Features", "Command", name)
        for field in ("pendingTab", "pendingModuleKey", "pendingReviewID", "pendingAskPrompt", "pendingDailyReport"):
            assert field not in src, f"{name} sets the router's {field}"
    assert "SystemEntry.open(path)" in _read(APP, "Features", "Command", "CommandSheet.swift")


def test_the_command_sheet_lives_inside_the_unlocked_tabs():
    root = _read(APP, "RootView.swift")
    tabs = root.split("private var mainTabs: some View {", 1)[1]
    tabs = tabs.split("\n    }\n", 1)[0]
    assert ".cavnarCommandSheetHost()" in tabs
    assert root.count(".cavnarCommandSheetHost()") == 1
    assert "CommandSheetRequest.request()" in _read(APP, "Features", "Modules", "ModulesGridView.swift")


def test_proposal_card_is_one_card_for_every_proposal():
    ask = _read(APP, "Features", "AskCavnar", "AskCavnarView.swift")
    assert "\nstruct ProposalCard: View" in ask
    assert "private struct ProposalCard" not in ask


# ── #18 / #19: Labor ─────────────────────────────────────────────────────────

def test_waiting_on_you_sits_directly_under_the_labor_hero():
    labor = _read(APP, "Features", "Labor", "LaborView.swift")
    hero = labor.index("heroCard(stats)\n")
    waiting = labor.index("LaborWaitingOnYou(viewModel: viewModel, setupViewModel: setupViewModel)")
    diagnosis = labor.index("LaborDiagnosisCard(diagnosis: diagnosis)")
    assert hero < waiting < diagnosis
    block = _read(APP, "Features", "Labor", "LaborWaitingOnYou.swift")
    # The same decide routes as the sections below, not a new path.
    assert "viewModel.decideTimeOff(req.id, approve:" in block
    assert "setupViewModel.decideShiftRequest(req.id, approve:" in block
    assert "if total > 0" in block, "hidden when nothing is waiting"


def test_send_is_pinned_once_a_week_is_drafted():
    labor = _read(APP, "Features", "Labor", "LaborView.swift")
    assert ".safeAreaInset(edge: .bottom)" in labor and "LaborSendBar(" in labor
    # One primary: the inline Send only remains for a result the bar can't send.
    inline = labor.split("let unsaved = viewModel.hasUnsavedFixes || viewModel.optimizerUnsaved\n", 1)[1][:600]
    assert "if result.historyId == nil" in inline
    bar = _read(APP, "Features", "Labor", "LaborWaitingOnYou.swift").split("struct LaborSendBar", 1)[1]
    assert "CavnarPrimaryButtonStyle(isDisabled: unsaved)" in bar and ".disabled(unsaved)" in bar


# ── #28: Food Cost ───────────────────────────────────────────────────────────

def test_food_cost_actions_sit_above_both_sub_tabs():
    fc = _read(APP, "Features", "FoodCost", "FoodCostQuickEntryView.swift")
    body = fc.split("var body: some View {", 1)[1]
    assert body.index("FoodCostActionRow(") < body.index("if subTab == .tracker {")
    # The route carries where inside Food Cost (F3-7); the section inbox
    # that raced it is gone.
    assert "FoodCostAction(path: focus)" in fc


def test_invoices_scan_with_the_camera_and_keep_the_photo_fallback():
    sheet = _read(APP, "Features", "FoodCost", "InvoiceScanSheet.swift")
    assert "DocumentCameraView" in sheet and "PhotosPicker(" in sheet
    assert "DocumentCameraView.isAvailable" in sheet
    cam = _read(APP, "Features", "FoodCost", "DocumentCameraView.swift")
    assert "VNDocumentCameraViewController" in cam
    # More pages than the server reads is said, never silently dropped.
    assert "extraPagesNote" in sheet


# ── #25: one person record ───────────────────────────────────────────────────

def test_the_person_sheet_uses_the_people_routes_and_is_reachable_everywhere():
    sheet = _read(APP, "Features", "People", "PersonSheet.swift")
    assert '"/mobile/api/people"' in sheet and '"/mobile/api/people/"' in sheet
    assert "method: .post" in sheet
    for path in (("Features", "Labor", "RosterSection.swift"),
                 ("Features", "Account", "AccountStaffDetailView.swift"),
                 ("Features", "Labor", "LaborWaitingOnYou.swift"),
                 ("Features", "Command", "CommandSheet.swift")):
        assert "PersonSheet(target:" in _read(APP, *path), "/".join(path)


# ── #41: Marketing ───────────────────────────────────────────────────────────

def test_marketing_posts_to_every_connected_channel_behind_one_confirm():
    view = _read(APP, "Features", "Marketing", "MarketingView.swift")
    social = view.split("private var socialPublish: some View {", 1)[1].split("private var googlePublish", 1)[0]
    assert social.count("CavnarPrimaryButtonStyle(") == 1, "exactly one primary"
    assert ".confirmationDialog(" in social
    assert "viewModel.postToAll(" in social
    assert '"Post to Instagram"' not in social and '"Post to Facebook"' not in social


# ── #49: the staff portal's requests ─────────────────────────────────────────

def test_the_ios_staff_portal_asks_for_time_off_and_swaps():
    portal = _read(APP, "Features", "Staff", "StaffPortalView.swift")
    assert 'case requests = "Requests"' in portal
    assert "StaffShiftChangeSheet(day:" in portal
    views = _read(APP, "Features", "Staff", "StaffRequestsViews.swift")
    server = _read(ROOT, "staff_routes.py")
    for route in ("/api/time-off", "/api/shift-requests", "/api/colleagues"):
        assert f'"/staff{route}"' in views
        assert f'@staff_bp.route("{route}"' in server
    assert '"swap"' in views and "target_name" in views


# ── routes the new Swift calls that exist today ──────────────────────────────

def test_every_existing_route_the_new_code_calls_is_on_the_server():
    """The command/people routes are other workstreams' (N, O1) and are
    guarded by a 404 fallback; everything else must already exist."""
    strategy = _read(ROOT, "strategy_routes.py")
    for path in ('"/actions"', '"/actions/pending"', '"/actions/<int:action_id>/cancel"',
                 '"/labor/time-off/<int:request_id>/decide"', '"/labor/shift-requests/<int:request_id>/decide"',
                 '"/dsr"', '"/dsr/<day>"'):
        assert path in strategy, path
    assert "strategy_mobile_bp.add_url_rule(_path" in strategy
