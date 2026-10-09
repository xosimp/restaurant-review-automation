"""Blind re-audit of the iPhone's Reviews and Marketing (10/8/26), fix round.

Source-level pins for the rules the fixes protect; the behaviour is unit
tested in Swift (CavnarAITests/ReauditReviews1008Tests.swift), which this
suite cannot run.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _src(*parts):
    with open(os.path.join(APP, *parts), encoding="utf-8") as f:
        return f.read()


def _between(src, start, end):
    body = src[src.index(start):]
    return body[:body.index(end)]


# ── H1: a swipe's approve is a bound, checked bulk approve behind a confirm ──

def test_the_swipe_confirms_and_posts_through_the_bulk_route():
    lst = _src("Features", "Reviews", "ReviewsListView.swift")
    swipe = _between(lst, ".swipeActions(edge: .trailing", ".listRowBackground")
    assert "swipeConfirm = review" in swipe, "the swipe opens the confirm, it never posts"
    assert "quickApprove(" not in swipe
    assert "BulkApproveConfirmSheet(reviews: [review]" in lst
    vm = _src("Features", "Reviews", "ReviewsListViewModel.swift")
    quick = _between(vm, "func quickApprove(_ review: Review)", "static func quickOutcome")
    # approve-all with review_ids + review_hashes: drafter.check_reply runs and
    # the action is bulk_approved, never approved_as_is (auto-approve trust).
    assert '"/mobile/api/reviews/approve-all"' in quick
    assert "Self.bulkApproveBodies([review])" in quick
    assert "/approve\"" not in quick
    # Select mode's bulk bar is the web's now; Publish N ready stays.
    assert "bulkBar" not in lst and "EditMode" not in lst
    assert "publishReadyPill(ready)" in lst


# ── H2 / H3: the queue names where a reply goes and posts once connected ─────

def test_the_queue_advances_unless_google_refused_and_posts_when_connected():
    review = _src("Models", "Review.swift")
    assert "var advancesQueue: Bool { ok && !failedOnGoogle }" in review
    vm = _src("Features", "Reviews", "ReviewDetailViewModel.swift")
    assert "didComplete = response.advancesQueue" in vm
    detail = _src("Features", "Reviews", "ReviewDetailView.swift")
    step = _between(detail, "private var approvedNextStep: some View {", "private func copyAndOpenYelp()")
    assert "viewModel.googleConnected == true" in step and "confirmingPostToGoogle = true" in step
    assert 'NavPath("account/integrations")' in step
    assert '"Post this reply to Google?"' in detail and "viewModel.retryPost()" in detail


# ── H7: consent never weakens — a number is only sent with consent ───────────

def test_a_review_request_texts_only_with_recorded_consent():
    sheet = _src("Features", "Reviews", "SendReviewRequestSheet.swift")
    assert 'private var phoneToSend: String { smsConsent ? phone : "" }' in sheet
    assert "phone: phoneToSend," in sheet
    assert "phone: phone," not in sheet.split("func send(", 1)[1].split("struct SendReviewRequestSheet", 1)[1]
    assert "smsConsent = false" in sheet, "a picked contact's number has agreed to nothing"


# ── H4: Schedule queues every selected target, only when Post could go ──────

def test_schedule_takes_the_selected_targets():
    view = _src("Features", "Marketing", "MarketingView.swift")
    assert "schedulePlatform =" not in view, "one platform picked for every schedule"
    actions = _between(view, "private var composeActions: some View {", "// MARK: - Publish")
    assert "let targets = scheduleTargets" in actions
    assert "targets.isEmpty || viewModel.isOverLimit" in actions
    sheet = _src("Features", "Marketing", "MarketingComposeViews.swift")
    assert "let platforms: [String]" in sheet
    assert "for platform in platforms where !done.contains(platform)" in sheet


# ── H5 / H6: the Studio never drops a drafted campaign without asking ───────

def test_the_studio_asks_before_losing_a_drafted_campaign():
    studio = _src("Features", "Marketing", "CampaignStudioView.swift")
    assert '.accountSheetChrome("Campaign Studio", isDirty: vm.hasUnsentWork)' in studio
    assert '"Replace your edits?"' in studio and "vm.create(redraft: true)" in studio
    assert '"Draft again"' not in studio
    vm = _src("Features", "Marketing", "CampaignStudioViewModel.swift")
    assert "resetDraft(keepPhoto: redraft)" in vm
    assert "if !keepPhoto { photos.clearMedia() }" in vm


# ── M3 / M4: approve only posts; a login that can't publish can't send ──────

def test_text_and_email_drafts_are_never_offered_an_approve():
    models = _src("Features", "Marketing", "MarketingComposeModels.swift")
    assert "var canApprove: Bool { !isApproved && !isExpired && !isGuestMessage }" in models


def test_publishing_follows_may_publish():
    view = _src("Features", "Marketing", "MarketingView.swift")
    assert '"Save for the owner to send"' in view
    assert "canPublish: canPublish" in view
    studio = _src("Features", "Marketing", "CampaignStudioView.swift")
    assert "|| !canPublish)" in studio


# ── M6: the attribution link lands on the card ──────────────────────────────

def test_the_attribution_link_has_a_web_section():
    with open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8") as f:
        page = f.read()
    assert 'id="mkt-attr-card" data-nav="marketing/attribution"' in page
    section = _src("Features", "Marketing", "MarketingAnalyticsSection.swift")
    assert 'path: "marketing/attribution"' in section


# ── M13: analytics stops fetching what the phone doesn't render ─────────────

def test_reviews_analytics_fetches_only_what_it_renders():
    vm = _src("Features", "Reviews", "ReviewsAnalyticsViewModel.swift")
    for path in ("/mobile/api/reviews/response-performance", "/mobile/api/reviews/sentiment-trend",
                 "/mobile/api/reviews/topic-weeks"):
        assert path not in vm, path
    # A new period re-reads the topics, never the whole tab (the read too).
    window = _between(vm, "func setWindow(_ days: Int) async {", "\n    }\n")
    assert "await load()" not in window


# ── L1 / L3 / M18 / L2: a delete or a cancel only says done when it is ──────

def test_deletes_and_cancels_wait_for_the_server():
    compose = _src("Features", "Marketing", "MarketingComposeViewModel.swift")
    delete = _between(compose, "func deleteDraft(_ draft: MarketingDraft) async {", "\n    }\n")
    assert "guard response.ok else" in delete and "try?" not in delete
    cancel = _between(compose, "func cancel(_ post: ScheduledPost) async {", "\n    }\n")
    assert "try?" not in cancel and "scheduleError" in cancel
    detail = _src("Features", "Reviews", "ReviewDetailViewModel.swift")
    tmpl = _between(detail, "func deleteTemplate(_ template: ResponseTemplate) async -> Bool {", "\n    }\n")
    assert "try?" not in tmpl and "guard response.ok else { return false }" in tmpl
    club = _src("Features", "Marketing", "GuestTextClubViewModel.swift")
    visit = _between(club, "func markVisit(_ contact: GuestContact) async {", "\n    }\n")
    assert "try?" not in visit.split("Task {", 1)[0]
    assert visit.index("guard response.ok else") < visit.index("Haptic.success()")
