"""iOS blind re-audit, Home + Ask (10/8/26): source-level pins for the rules
the fix round put in — each would read as an unrelated regression if lost.
(H1's server half is in tests/test_ask_iphone_card.py.)"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _src(*parts):
    with open(os.path.join(APP, *parts), encoding="utf-8") as f:
        return f.read()


def test_not_now_on_a_proposal_is_never_a_green_done():
    """H2: "Not now" dismisses — it says "Not sent" in Ink2, no check."""
    ask = _src("Features", "AskCavnar", "AskCavnarView.swift")
    assert "case pending, working, done, dismissed, failed, uncertain" in ask
    dismiss = ask.split("private func dismissNow(", 1)[1].split("\n    }\n", 1)[0]
    assert "phase = .dismissed" in dismiss and "phase = .done" not in dismiss
    assert 'Text("Not sent")' in ask


def test_home_leads_with_the_decision_and_open_recs_are_needs_you_rows():
    """H3 / H7 / M19 / H6."""
    home = _src("Features", "Home", "HomeView.swift")
    body = home.split("var body: some View", 1)[1]
    assert body.index("hero(summary)") < body.index("HomeOneThingCard(") < body.index("HomeKPIRow(")
    assert "findOrAsk\n" not in body and "findOrAskButton" in body
    assert "recommendations: recommendationsShown(summary, lead: lead)" in home
    more = home.split("private func moreGroup(", 1)[1].split("\n    }\n", 1)[0]
    assert "HomeRecommendations(" not in more and "DNAHomeCard(model: dnaModel)" in more
    needs = _src("Features", "Home", "HomeNeedsYou.swift")
    assert "case recommendation(HomeRecommendation)" in needs
    assert "ConfidenceLine(confidence: c, recKey: rec.key" in needs


def test_the_home_badge_is_needs_you_for_this_location():
    """H8."""
    root = _src("RootView.swift")
    assert ".badge(homeViewModel.needsYouCount)" in root
    assert "urgentCount: homeViewModel.needsYouCount" in root
    assert "onCount: { count in viewModel.needsYouCount = count }" in _src("Features", "Home", "HomeView.swift")


def test_the_bell_is_history_and_approve_names_where_it_posts():
    """M3 / M5 / H4 / L14."""
    bell = _src("Features", "Notifications", "NotificationsListView.swift")
    assert 'Text("Needs you").tag(true)' not in bell
    assert 'NavPath("home/needs")' in bell
    assert 'rowAction("Approve", tint: .cavnarEmber2) { approving = item }' in bell
    assert '"Post this reply to Google?"' in bell
    assert "Text(item.title)" in bell and "item.snippet" in bell
    assert "AIActivityStrip(viewModel: activity, paused: true)" in bell
    item = _src("Models", "NotificationItem.swift")
    assert 'case postsTo = "posts_to"' in item and "location" in item
    sheet = _src("Features", "Command", "CommandSheet.swift")
    assert 'go("home/needs")' in sheet and "waitingRow(" not in sheet


def test_a_chat_is_never_deleted_on_a_full_swipe():
    """M6."""
    hist = _src("Features", "AskCavnar", "AskCavnarHistoryView.swift")
    assert "allowsFullSwipe: true" not in hist
    assert ".confirmationDialog(" in hist and "swipe left to delete" not in hist


def test_the_focus_cards_answer_leads_and_its_toast_clears():
    """L7."""
    card = _src("Features", "Home", "HomeOneThingCard.swift")
    assert 'primaryButton("Done", working: answering)' in card
    assert ".task(id: toast)" in card and "toast = nil" in card


def test_the_dna_screen_sets_type_by_role():
    """H5 / H9: no literal sizes and one figureXL on the DNA screen."""
    for name in ("HomeDNA.swift", "HomeDNAStory.swift"):
        src = _src("Features", "Home", name)
        assert not re.search(r"\bcavnar(?:Body|Headline|Number)\(\s*\d", src), name
        assert "minimumScaleFactor(0.6)" not in src, name
    screen = _src("Features", "Home", "HomeDNA.swift")
    assert screen.count(".cavnarText(.figureXL)") == 1
    assert 'path: "dna"' in screen
    story = _src("Features", "Home", "HomeDNAStory.swift")
    assert ".cavnarText(.figureM)" in story


def test_web_paths_the_phone_links_to_are_routed():
    """M15 / M18: ?nav=dna and ?nav=locations open real places."""
    with open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8") as f:
        web = f.read()
    assert "cavNavRegister('dna'" in web and "cavNavRegister('locations'" in web
