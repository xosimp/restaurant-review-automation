"""The iPad layout (iOS parity audit #99, 10/7/26): the app runs on iPad in
every orientation and in Split View, a regular width gets a sidebar while a
compact one keeps the phone's tab bar, and the iPhone stays portrait-only."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI")
APP = os.path.join(IOS, "CavnarAI")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_every_target_runs_on_iphone_and_ipad():
    yml = _read(IOS, "project.yml")
    assert 'TARGETED_DEVICE_FAMILY: "1"\n' not in yml
    assert yml.count('TARGETED_DEVICE_FAMILY: "1,2"') == 4


def test_the_ipad_turns_every_way_and_the_iphone_stays_portrait():
    yml = _read(IOS, "project.yml")
    phone = yml.split("UISupportedInterfaceOrientations:\n", 1)[1].split("UISupportedInterfaceOrientations~ipad:", 1)[0]
    assert "UIInterfaceOrientationPortrait" in phone and "Landscape" not in phone
    pad = yml.split("UISupportedInterfaceOrientations~ipad:\n", 1)[1].split("UIRequiresFullScreen", 1)[0]
    for o in ("Portrait", "PortraitUpsideDown", "LandscapeLeft", "LandscapeRight"):
        assert f"UIInterfaceOrientation{o}\n" in pad, o
    assert "UIRequiresFullScreen: false" in yml
    assert "CFBundleIcons~ipad:" in yml


def test_a_regular_width_gets_the_sidebar_and_a_compact_one_the_tab_bar():
    root = _read(APP, "RootView.swift")
    main = root.split("private var mainTabs: some View {", 1)[1].split("\n    }\n", 1)[0]
    assert "CavnarLayout.usesSidebar(sizeClass)" in main
    assert "splitShell" in main and "tabShell" in main
    assert "NavigationSplitView {" in root and "AppSidebar(selection: sidebarSelection" in root
    # One state for both shapes: the sidebar sets the tab and the paths the
    # router already drives.
    body = root.split("private func openFromSidebar(", 1)[1].split("\n    }\n", 1)[0]
    assert "selectedTab = tab" in body and "modulesPath = fresh" in body
    staff = _read(APP, "Features", "Staff", "StaffPortalView.swift")
    assert "CavnarLayout.usesSidebar(sizeClass)" in staff and "NavigationSplitView {" in staff


def test_keyboard_commands_reach_the_tabs_ask_and_refresh():
    root = _read(APP, "RootView.swift")
    for piece in ("CommandSheetRequest.request()", "askCavnarViewModel.startNewChat()",
                  "CavnarKeyCommand.refresh", "tab.shortcutDigit"):
        assert piece in root, piece
    motion = _read(APP, "DesignSystem", "CavnarMotion.swift")
    refresh = motion.split("struct CavnarEmberRefreshable: ViewModifier {", 1)[1].split("\n}\n", 1)[0]
    assert "CavnarKeyCommand.refresh" in refresh and "guard onScreen" in refresh


def test_the_labor_week_grid_edits_through_the_existing_sheet():
    week = _read(APP, "Features", "Labor", "ScheduleWeekViews.swift")
    assert "struct ScheduleWeekGrid: View" in week
    assert "wide ? [.week, .day, .person] : [.day, .person]" in week
    labor = _read(APP, "Features", "Labor", "LaborView.swift")
    assert "onEditShift: viewModel.weekReadOnlyReason == nil ? { editingShift = .edit($0) } : nil" in labor


def test_the_waiting_widget_has_a_large_size():
    widget = _read(IOS, "CavnarWidgets", "CavnarWaitingWidget.swift")
    assert ".systemLarge" in widget and "case .systemLarge:" in widget
