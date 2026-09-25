"""iOS navigation, notifications, Home and Reviews — friction audit (9/25/26)
items 3, 9, 11, 15, 21, 22, 32, 50.

The behaviour itself is unit-tested in Swift (CavnarAITests/
FrictionNavigationTests.swift), which this suite cannot run. These pin the
wiring in the sources so a refactor can't quietly drop it: where a link
lands, that the lock asks Face ID on its own, that Home leads with the work,
that the review queue moves on, and that the bell and switcher are reachable
from every screen.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _src(rel):
    with open(os.path.join(APP, rel), encoding="utf-8") as f:
        return f.read()


def test_one_router_opens_every_nav_path():
    root = _src("RootView.swift")
    assert "publisher(for: .cavnarOpenNav)" in root
    assert "deepLinkRouter.open(nav)" in root
    router = _src("Push/DeepLinkRouter.swift")
    # "action" and "proposal" share a case: two id spaces, two sheets (F3-2).
    for head in ('case "action", "proposal":', 'case "dsr":', 'case "ask":', 'case "location":'):
        assert head in router, head
    # A push's own nav wins over the module mirror.
    assert "if let path = NavPath(nav)" in router


def test_a_pending_send_opens_its_undo_sheet():
    assert "PendingActionSheet(actionId: ref.id)" in _src("RootView.swift")
    sheet = _src("Features/Notifications/PendingActionSheet.swift")
    assert "/mobile/api/actions/\\(action.id)/cancel" in sheet


def test_module_screens_receive_their_focus():
    dest = _src("Features/Modules/ModuleDestinationView.swift")
    assert "ReviewsListView(initialFilter: route?.filter" in dest
    assert "LaborView(focusSection: route?.section, focusItem: route?.itemId)" in dest
    assert "CavnarBellButton()" in dest
    assert "cavnarShowsLocationTitle, true" in dest
    labor = _src("Features/Labor/LaborView.swift")
    assert "revealFocus(proxy: proxy)" in labor
    assert "ScheduleHistoryView()" in labor


def test_a_deep_link_starts_a_fresh_modules_stack():
    grid = _src("Features/Modules/ModulesGridView.swift")
    assert "var fresh = NavigationPath()" in grid
    assert '"waitlist"' not in grid and '"bar"' not in grid


def test_the_warm_lock_asks_face_id_on_its_own():
    root = _src("RootView.swift")
    assert "autoUnlockIfWarm()" in root
    assert "guard coldLaunch else" in root
    prefs = _src("Core/AppPreferences.swift")
    assert "defaultLockDelaySeconds = 60" in prefs
    assert "initialLockDelay(stored: d.object(forKey: Key.lockDelay))" in prefs


def test_home_leads_with_the_work():
    home = _src("Features/Home/HomeView.swift")
    body = home.split("var body: some View", 1)[1]
    attention = body.index("attentionSection(summary)")
    assert attention < body.index("HomeValueBand(")
    assert attention < body.index("HomeBenchmarkStrip(")
    assert attention < body.index("HomeLastNightCard(")
    # Every item, not the first four behind a swipe.
    assert "items: summary.needsAttention," in home
    deck = _src("Features/Home/HomeActionDeck.swift")
    assert "+\\(split.hidden) more" in deck
    assert "DragGesture" not in deck


def test_ask_about_this_sends():
    assert "router.pendingAskAutoSend = true" in _src("Features/Home/HomeAskLink.swift")
    root = _src("RootView.swift")
    assert "await askCavnarViewModel.submit()" in root


def test_review_queue_mode():
    detail = _src("Features/Reviews/ReviewDetailView.swift")
    assert ".safeAreaInset(edge: .bottom)" in detail
    assert '"Approve & next"' in detail
    lst = _src("Features/Reviews/ReviewsListView.swift")
    assert ".swipeActions(edge: .trailing" in lst
    assert "ReviewsListViewModel.canQuickApprove(review)" in lst
    vm = _src("Features/Reviews/ReviewsListViewModel.swift")
    assert "filter = .toApprove" in vm


def test_notification_rows_act_in_place():
    view = _src("Features/Notifications/NotificationsListView.swift")
    assert 'rowAction("Undo"' in view and 'rowAction("Approve"' in view
    assert "nav: item.nav" in view
    # The sheet no longer waits on the network before it appears.
    chrome = _src("Core/AppChrome.swift")
    assert "showingNotifications = true\n        Task { await notificationsList.load() }" in chrome


def test_the_bell_and_the_switcher_reach_every_screen():
    root = _src("RootView.swift")
    assert ".badge(chrome.notificationsBadge.unreadCount)" in root
    # The reset runs for EVERY switch path, from SessionStore (F3-4).
    assert "LocationSwitcherView {}" in root
    assert "session.onLocationSwitched = { _ in didSwitchLocation() }" in root
    assert "CavnarScreenTitle(title: title)" in _src("DesignSystem/ViewModifiers.swift")
    # A store switch no longer replays the landing intro.
    session = _src("Core/SessionStore.swift")
    switch_body = session.split("func didSwitchLocation", 1)[1].split("\n    }\n", 1)[0]
    assert not re.search(r"hasShownHomeIntro\s*=\s*false", switch_body)


def test_toolbar_icons_are_44pt_targets():
    mods = _src("DesignSystem/ViewModifiers.swift")
    glass = mods.split("func cavnarToolbarIconGlass", 1)[1][:600]
    assert "max(44, size)" in glass
