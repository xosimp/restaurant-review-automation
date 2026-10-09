import SwiftUI

private enum ReviewsSubTab: String, CaseIterable, Identifiable {
    case inbox = "Inbox"
    case analytics = "Analytics"
    var id: String { rawValue }
}

struct ReviewsListView: View {
    @State private var viewModel = ReviewsListViewModel()
    @State private var analyticsViewModel = ReviewsAnalyticsViewModel()
    @State private var deepLinkedReview: Review?
    @State private var subTab: ReviewsSubTab = .inbox
    @State private var showingSendRequest = false
    @State private var clock = CavnarEntranceClock()
    @State private var analyticsLoaded = false
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    /// Where the link that opened this was pointing (friction audit #3):
    /// "reviews?filter=urgent" opens on Urgent, "review/412" on that review.
    var initialFilter: String? = nil
    var focusReviewId: Int? = nil
    /// A review the loaded page doesn't hold (older, or another filter) —
    /// fetched by id rather than silently not opening.
    @State private var fetchedReviewId: Int?
    /// What a swipe-approve did to its row, in a sentence: a failure (red)
    /// or the next step for a reply that is approved but not live (ink).
    @State private var rowNote: (id: Int, message: String, isError: Bool)?
    @State private var approvingRowId: Int?
    @State private var postedLabel: String?
    @State private var focusConsumed = false
    /// The review a swipe's Approve is confirming: the whole reply, where it
    /// goes, then Approve (re-audit 10/8/26 H1). The row shows two lines of
    /// it; one tap used to publish it to Google unread.
    @State private var swipeConfirm: Review?
    /// "Publish N ready": Home's publish confirm, from the inbox chips.
    @State private var showingPublishReady = false
    /// Search sits behind the toolbar's magnifier until it is wanted
    /// (re-audit 10/8/26 M16) — it cost a row above the first review.
    @State private var showingSearch = false
    @FocusState private var searchFocused: Bool

    init(initialFilter: String? = nil, focusReviewId: Int? = nil) {
        self.initialFilter = initialFilter
        self.focusReviewId = focusReviewId
    }

    var body: some View {
        // No NavigationStack of its own — this is now a pushed destination
        // inside Home's or the Modules tab's stack, not a tab root, so it
        // shares whichever stack pushed it in.
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: ReviewsSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 16)

            Group {
                if subTab == .inbox {
                    inboxContent
                } else {
                    ReviewsAnalyticsSection(viewModel: analyticsViewModel)
                }
            }
        }
        .cavnarModuleBackground()
        .navigationTitle("Reviews")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Reviews") }
        .cavnarTabSwipeNavigation($subTab, primaryTab: .inbox, secondaryTab: .analytics)
        .toolbar {
            // Select mode's bulk approve / skip is the web's now (re-audit
            // 10/8/26 W3): "Publish N ready" covers approving many here.
            if subTab == .inbox {
                cavnarToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.2)) {
                            if showingSearch {
                                showingSearch = false
                                viewModel.searchText = ""
                            } else {
                                showingSearch = true
                                searchFocused = true
                            }
                        }
                    } label: {
                        Image(systemName: showingSearch ? "xmark" : "magnifyingglass")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                            .cavnarToolbarIconGlass()
                    }
                    .buttonStyle(.plain)
                    .tint(nil)
                    .accessibilityLabel(showingSearch ? "Close search" : "Search reviews")
                }
            }
            cavnarToolbarItem(placement: .navigationBarTrailing) {
                Button {
                    Haptic.light()
                    showingSendRequest = true
                } label: {
                    // envelope.badge's glyph reserves extra bounding-box
                    // space for the notification dot, off to one side —
                    // that's what made it look both oversized AND
                    // off-center no matter how it was framed, since the
                    // frame centers the glyph's reported bounding box, not
                    // its visual weight. Plain envelope has no such
                    // reserved space (and reads better semantically too —
                    // this button composes/sends a request, it isn't
                    // indicating unread mail).
                    Image(systemName: "envelope")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber)
                        .cavnarToolbarIconGlass()
                }
                .buttonStyle(.plain)
                .tint(nil)
                .accessibilityLabel("Ask a guest for a review")
            }
        }
        .sheet(isPresented: $showingSendRequest) {
            SendReviewRequestSheet()
        }
        .sheet(isPresented: $showingPublishReady) {
            PublishReadySheet(count: viewModel.stats?.publishable ?? 0) { label in
                postedLabel = label
                Task { await viewModel.reload() }
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        // A swipe's Approve asks first: the whole reply in the words that
        // would post, and where it goes (re-audit 10/8/26 H1, M1).
        .sheet(item: $swipeConfirm) { review in
            BulkApproveConfirmSheet(reviews: [review], held: 0,
                                    isWorking: approvingRowId != nil) {
                quickApprove(review)
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .navigationDestination(item: $deepLinkedReview) { review in
            ReviewDetailView(
                viewModel: ReviewDetailViewModel(review: review),
                onCompleted: { status in viewModel.markCompleted(reviewID: review.id, status: status) },
                // Queue mode (friction audit #21): after an approve the
                // detail moves on to the next reply waiting, in the order
                // the list shows them, instead of popping back here.
                nextInQueue: { id in viewModel.nextInQueue(after: id) },
                onAdvanced: { status, id in viewModel.markCompleted(reviewID: id, status: status) }
            )
        }
        .navigationDestination(item: $fetchedReviewId) { id in
            ReviewByIdView(reviewID: id, category: nil)
        }
        .task {
            // Opens on the link's filter, else on "To approve" whenever
            // replies are waiting — the inbox used to open on All and the
            // owner tapped the chip every time (friction audit #21).
            await viewModel.openInbox(preferred: ReviewInboxFilter(key: initialFilter))
            openDeepLinkIfNeeded()
        }
        .cavnarPostedOverlay(postedLabel) { postedLabel = nil }
        // Reopening the app after a while re-reads the inbox rather than
        // showing this morning's list as current (audit 4.2).
        .refreshOnForeground(lastLoaded: viewModel.lastLoadedAt) { await viewModel.reload() }
        // Analytics is five requests, one of them an LLM call. It used to
        // fire on every Reviews open whether or not the owner ever looked
        // at the tab — so the app paid for a Haiku insight per visit to the
        // inbox. Loads when the tab is actually selected, once.
        .onChange(of: subTab) { _, tab in
            guard tab == .analytics, !analyticsLoaded else { return }
            analyticsLoaded = true
            Task { await analyticsViewModel.load() }
        }
    }

    @ViewBuilder
    private var inboxContent: some View {
        Group {
            if viewModel.reviews.isEmpty && !viewModel.isLoading, let error = viewModel.errorMessage {
                // A failed load is not an empty inbox (CLIENT-30): it used to
                // hide the empty state and then render nothing at all.
                VStack(spacing: 8) {
                    Text(error).cavnarText(.secondary)
                        .multilineTextAlignment(.center)
                    Button("Retry") { Task { await viewModel.reload() } }
                }
                .padding(.horizontal, 24)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if viewModel.reviews.isEmpty && !viewModel.isLoading && viewModel.filter == .all {
                CavnarEmptyHearth(
                    title: "No reviews yet",
                    message: "New reviews land here automatically once your platforms are connected."
                )
            } else {
                List {
                    // Same chips as the web inbox, plus search — the web
                    // had both and the phone had neither.
                    Section {
                        // How current the inbox is — the real fetch state,
                        // not the reviews_live flag (DH4-6) — only when it
                        // is news: a missed check (amber) or a stopped one.
                        // On schedule it said nothing an owner acts on and
                        // pushed the figures down (10/8/26).
                        if let fetch = viewModel.fetchLine, fetch.tone == "warn" || fetch.tone == "bad" {
                            ServerStatusCaption(status: fetch)
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 8, trailing: 16))
                        }
                        // One set of inbox counts (readability round 10/8/26
                        // #55): the why line leads — what the reviews are
                        // about, in one sentence — then the rating and how
                        // much is answered. The "to approve" and "urgent"
                        // counts ride the chips below, and the "N new this
                        // month · avg reply" line is the web's.
                        if let stats = viewModel.stats {
                            // What the reviews are about, in one line
                            // (density #32) — the server's why line (the
                            // rating's move, the stored top complaint) on
                            // any chip; an older server's fallback reads the
                            // newest reviews the "All" list holds. A tap
                            // opens the urgent ones, or the analysis.
                            if let why = ReviewsWhyLine.make(urgent: stats.urgent, server: viewModel.why)
                                ?? (viewModel.filter == .all
                                    ? ReviewsWhyLine.make(urgent: stats.urgent, reviews: viewModel.reviews) : nil) {
                                Button {
                                    Haptic.light()
                                    withAnimation(.easeOut(duration: 0.2)) {
                                        if stats.urgent > 0 { viewModel.filter = .urgent } else { subTab = .analytics }
                                    }
                                } label: {
                                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                                        CavnarMixedText(why, role: .lead,
                                                        color: stats.urgent > 0 ? .cavnarRedText : .cavnarInk)
                                        Text(stats.urgent > 0 ? "Open the urgent ones \u{2192}" : "See why \u{2192}")
                                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                            .foregroundStyle(Color.cavnarEmber2)
                                    }
                                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                .listRowBackground(Color.clear)
                                .listRowSeparator(.hidden)
                                .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: CavnarSpace.s, trailing: 16))
                            }
                            // The reputation summary. ReviewStats was modelled
                            // in full and then called from nowhere, so the
                            // phone's Reviews tab opened with no rating and no
                            // response rate while the web showed them.
                            ReviewsStatStrip(stats: stats)
                                .listRowBackground(Color.clear)
                                .listRowSeparator(.hidden)
                                .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: CavnarSpace.xs, trailing: 16))
                            // "Publish N ready" rides the chip row below
                            // (re-audit 10/8/26 M16).
                        }
                    }
                    Section {
                        inboxFilters
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 6, trailing: 16))
                    }
                    if let notice = viewModel.stalenessNotice {
                        CachedDataNotice(text: notice).listRowBackground(Color.clear).listRowSeparator(.hidden)
                    }
                    // What the last bulk approve or skip did, in counts.
                    if let note = viewModel.bulkNote {
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            CavnarMixedText(note.text, role: .secondary,
                                            color: note.failed ? .cavnarRedText : .cavnarInk2)
                            Spacer(minLength: 8)
                            Button {
                                Haptic.light()
                                viewModel.bulkNote = nil
                            } label: {
                                Image(systemName: "xmark")
                                    .font(.cavnar(.caption))
                                    .foregroundStyle(Color.cavnarInk2)
                                    .frame(width: 44, height: 44)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Dismiss")
                        }
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                    }
                    // A refresh or chip change that failed over rows already
                    // on screen: say so above them, with the way to retry.
                    if let error = viewModel.errorMessage {
                        HStack(spacing: 10) {
                            Text(error)
                                .cavnarText(.secondary, color: .cavnarRedText)
                            Spacer(minLength: 8)
                            Button("Retry") { Task { await viewModel.reload() } }
                                .font(.cavnar(.label))
                                .frame(minHeight: 44)
                        }
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                    }
                    if viewModel.filteredReviews.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                        Text(viewModel.searchText.isEmpty ? "No \(viewModel.filter.rawValue.lowercased()) reviews" : "Nothing matches \u{201C}\(viewModel.searchText)\u{201D}")
                            .cavnarText(.body)
                            .frame(maxWidth: .infinity)
                            .padding(.top, 30)
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    }
                    ForEach(Array(viewModel.filteredReviews.enumerated()), id: \.element.id) { index, review in
                    // A NavigationLink row plus a .simultaneousGesture tap
                    // haptic was tried here and broke navigation outright —
                    // the gesture ended up winning the hit-test in this List,
                    // so taps fired the haptic but never pushed. A plain
                    // Button driving the same .navigationDestination(item:)
                    // used for deep links fires the haptic AND navigates
                    // reliably, since there's only ever one gesture involved.
                    Button {
                        Haptic.light()
                        deepLinkedReview = review
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            ReviewRow(review: review)
                                .opacity(approvingRowId == review.id ? 0.5 : 1)
                            if let rowNote, rowNote.id == review.id {
                                Text(rowNote.message)
                                    .cavnarText(.secondary, color: rowNote.isError ? .cavnarRedText : .cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .cavnarRowEntrance(index: index, clock: clock)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    // The pointer highlights the row on an iPad (#99).
                    .hoverEffect(.highlight)
                    // Swipe to approve a reply that may be published unread
                    // (friction audit #21). A flagged, urgent or old draft
                    // has no swipe: it keeps the read-first rule. Not a FULL
                    // swipe: one flick published to Google with nothing in
                    // between (F3-14). The swipe opens the confirm with the
                    // whole reply and where it goes (re-audit 10/8/26 H1);
                    // the confirm's button is the decision.
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        if ReviewsListViewModel.canQuickApprove(review) && approvingRowId == nil {
                            Button {
                                Haptic.light()
                                swipeConfirm = review
                            } label: {
                                Label("Approve", systemImage: "checkmark")
                            }
                            .tint(Color.cavnarGreen)
                        }
                    }
                    // List rows keep an opaque background of their own even
                    // with .scrollContentBackground(.hidden) below (that only
                    // clears the list's overall canvas) — every other module
                    // uses a ScrollView with translucent glass cards, which
                    // is why only Reviews had a hard cutoff against the top
                    // gradient instead of blending into it.
                    .listRowBackground(Color.clear)
                    .listRowSeparatorTint(Color.cavnarPaper3)
                    // The next page loads when the last row appears rather
                    // than the whole history arriving on every refresh.
                    .task {
                        if review.id == viewModel.filteredReviews.last?.id {
                            await viewModel.loadMore()
                        }
                    }
                    }
                    if viewModel.isLoadingMore {
                        HStack { Spacer(); CavnarSkeletonBar(height: 3).frame(width: 180); Spacer() }
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .accessibilityLabel("Loading more reviews")
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        }
        .overlay {
            if viewModel.isLoading && viewModel.reviews.isEmpty { CavnarLoadingOrb() }
        }
        .cavnarEmberRefreshable { await viewModel.reload() }
        // Each chip is answered by the server over the whole inbox, not by
        // filtering the page already on the phone.
        .onChange(of: viewModel.filter) { _, _ in
            // The first open sets its filter before its own first load
            // (openInbox); only a chip tap after that reloads.
            guard viewModel.inboxOpened else { return }
            Task { await viewModel.reload() }
        }
    }

    /// "Publish 4 ready" — every reply one bulk publish may post, behind
    /// Home's own confirm card (the replies in their own words).
    private func publishReadyPill(_ ready: Int) -> some View {
        Button {
            Haptic.light()
            showingPublishReady = true
        } label: {
            HomeMixedText.make("Publish \(ready) ready", role: .label, color: .cavnarEmber2,
                               numberColor: .cavnarEmber2)
                .padding(.horizontal, CavnarSpace.m)
                .frame(minHeight: 44)
                .background(Color.cavnarPaper2.opacity(0.85), in: Capsule())
                .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.4), lineWidth: 1))
        }
        .buttonStyle(.plain)
        .accessibilityHint("Shows the replies before anything is posted")
    }

    private var inboxFilters: some View {
        VStack(spacing: 10) {
            if showingSearch || !viewModel.searchText.isEmpty {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(Color.cavnarInk3)
                TextField("Search reviews", text: Binding(
                    get: { viewModel.searchText }, set: { viewModel.searchText = $0 }))
                    .font(.cavnar(.body))
                    .autocorrectionDisabled()
                    .focused($searchFocused)
                if !viewModel.searchText.isEmpty {
                    Button {
                        Haptic.light()
                        viewModel.searchText = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(Color.cavnarInk3)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear search")
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(Color.cavnarPaper2)
            .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .clipShape(Capsule())
            }

            ScrollView(.horizontal) {
                HStack(spacing: 8) {
                    // The inbox's one bulk action leads the chips, behind
                    // Home's own confirm card (M16).
                    if let ready = viewModel.stats?.publishable, ready > 0 {
                        publishReadyPill(ready)
                    }
                    ForEach(ReviewInboxFilter.allCases) { f in
                        let on = viewModel.filter == f
                        let n = viewModel.count(for: f)
                        Button {
                            guard !on else { return }
                            Haptic.light()
                            withAnimation(.easeOut(duration: 0.2)) { viewModel.filter = f }
                        } label: {
                            // The chips carry the inbox's counts — "To approve
                            // 3", "Urgent 1" — said once, here (#55).
                            HStack(spacing: 5) {
                                Text(f.rawValue).font(.cavnarBody(CavnarType.secondary, weight: 700))
                                if let n, n > 0 {
                                    Text("\(n)").font(.cavnarNumber(CavnarType.secondary, weight: 700))
                                        .foregroundStyle(on ? Color.white.opacity(0.9) : Color.cavnarEmber2)
                                }
                            }
                            .foregroundStyle(on ? Color.white : Color.cavnarInk2)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(on ? Color.cavnarEmber : Color.cavnarPaper2)
                            .overlay(Capsule().strokeBorder(on ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
                            .clipShape(Capsule())
                            // The chip stays ~32pt; the tap area is 44pt
                            // (friction audit #50).
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.vertical, 2)
            }
            .scrollIndicators(.hidden)
            .scrollClipDisabled()
        }
    }

    /// Reads directly from the shared DeepLinkRouter (injected via
    /// .environment in CavnarAIApp) rather than an init parameter — Reviews
    /// is reached from two places now (Home's grid, the Modules tab), and a
    /// tapped notification needs to reach it either way without RootView
    /// having to construct this view itself.
    private func openDeepLinkIfNeeded() {
        // The router's (a push, a notification row) or this screen's own
        // route (a card on Home's stack). Either way it opens: from the
        // loaded page when it's there, fetched by id when it isn't — it used
        // to open nothing for a review past the first page.
        let routed = deepLinkRouter.consumePendingReviewID()
        // This screen's own focus opens once — not again every time the
        // owner comes back from it.
        let own = focusConsumed ? nil : focusReviewId
        focusConsumed = true
        guard let reviewID = routed ?? own else { return }
        if let match = viewModel.reviews.first(where: { $0.id == reviewID }) {
            deepLinkedReview = match
        } else {
            fetchedReviewId = reviewID
        }
    }

    /// A trailing swipe on a reply that may be published unread (the same
    /// bar as Home's "Publish N replies": drafted, not flagged, not urgent).
    /// Flagged drafts keep the read-first rule and have no swipe.
    ///
    /// After its confirm, the swipe goes through the bulk route pinned to
    /// this one reply and the words the confirm showed (re-audit 10/8/26
    /// H1): the server runs the public-reply check a bulk publish runs
    /// (drafter.check_reply), holds a flagged or rewritten draft, and records
    /// it as bulk-approved — nobody read it on its own screen, so it never
    /// counts toward auto-approve trust.
    private func quickApprove(_ review: Review) {
        guard approvingRowId == nil else { return }
        approvingRowId = review.id
        rowNote = nil
        Task {
            let outcome = await viewModel.quickApprove(review)
            approvingRowId = nil
            switch outcome {
            case .posted:
                postedLabel = "Reply posted to \(review.platformDisplayName)"
            case .approved:
                // Approved, not live: where it goes next, in ink — a Yelp
                // reply is a finished approve, not a failure (M2). A Google
                // reply whose post failed shows "Couldn't post" on its pill.
                rowNote = (review.id, Self.approvedNote(review), false)
            case .held(let why):
                // Held for a read since the list loaded: it is read, and
                // confirmed, on its own screen.
                rowNote = (review.id, why, true)
            case .changed:
                rowNote = (review.id, "This reply changed since you read it, so it wasn\u{2019}t posted. Open it to read the current one.", true)
            case .failed(let why):
                rowNote = (review.id, why, true)
            }
        }
    }

    /// The row's line under a reply that was approved but isn't live.
    static func approvedNote(_ review: Review) -> String {
        review.platform == "google"
            ? "Approved \u{00B7} not on Google yet \u{2014} open it to post."
            : "Approved \u{00B7} post it on \(review.platformDisplayName) \u{2014} open it to copy the reply."
    }
}

struct ReviewRow: View {
    let review: Review
    /// Off in Select mode, where a tap ticks the row instead of opening it.
    var showsChevron: Bool = true
    /// The severity chip's reason, shown on tap (models.severity_reason).
    @State private var showingSeverityReason = false

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                StarRatingView(rating: review.rating ?? 0)
                    .padding(.top, 3)
                VStack(alignment: .leading, spacing: CavnarSpace.xxs + 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(review.author ?? "Anonymous")
                            .cavnarText(.label)
                            .lineLimit(1)
                        if let date = review.formattedDate {
                            Text(date)
                                .font(.cavnarNumber(CavnarType.caption, weight: 500))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        // Urgent only while it waits on someone: an
                        // answered review is not urgent (the web card's
                        // `r.urgent and not _handled`, the server's rule).
                        if review.isUrgent && !review.isHandled {
                            Image(systemName: "exclamationmark.circle.fill")
                                .foregroundStyle(Color.cavnarRed)
                                .font(.cavnar(.caption))
                        }
                        // The severity tier, for the two tiers an owner must
                        // not scroll past. A tap says why — this review's
                        // own complaint, then what the tier means — the
                        // web chip's hover (severity_reason).
                        if review.isHighSeverity, let label = review.severityLabel {
                            severityChip(label)
                        }
                    }
                    // The handled state in the web card's words and tones
                    // (Live on Google, Replied on Google, Couldn't post to
                    // Google, Approved · not on Google yet, Skipped · no
                    // reply sent). Nothing while it still waits.
                    if let pill = review.statusPill {
                        StatusPill(label: pill.label, tone: pill.tone)
                    }
                    Text(review.text ?? "")
                        .cavnarText(.body)
                        .lineLimit(3)
                    // Cavnar's one-line read, which the analyser has written
                    // on every review since the product existed and which
                    // nothing on either platform ever showed.
                    if let complaint = review.specificComplaint, !complaint.isEmpty {
                        Text(complaint)
                            .cavnarText(.caption, color: .cavnarEmber2)
                            .lineLimit(1)
                    }
                    if !review.isAnalysed {
                        // Unanalysed reviews reach the inbox now; saying so
                        // beats showing the "neutral" sentiment they were
                        // never actually assigned.
                        Text("Analysis pending")
                            .cavnarText(.caption)
                    }
                    // The reply Cavnar AI drafted, two lines of it, so the
                    // owner can approve from the list (swipe) without
                    // opening the review (readability round 10/8/26 #17).
                    // Only for a draft that may go out unread: a flagged
                    // draft keeps the read-first rule and shows none.
                    if let draft = Self.draftPreview(review) {
                        Text(draft)
                            .cavnarText(.secondary)
                            .lineLimit(2)
                            .padding(.leading, CavnarSpace.s)
                            .overlay(alignment: .leading) {
                                Rectangle().fill(Color.cavnarEmber.opacity(0.7)).frame(width: 2)
                            }
                            .padding(.top, 2)
                            .accessibilityLabel("Drafted reply: \(draft)")
                    }
                }
            }
            Spacer(minLength: 0)
            // Manual chevron — the row is a Button now, not a NavigationLink,
            // so there's no free system disclosure indicator anymore. It
            // sits in its own outer HStack(alignment: .center) so it stays
            // vertically centered regardless of how tall the top-aligned
            // content beside it grows.
            if showsChevron {
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(.vertical, CavnarSpace.xxs)
        // Handled reviews sit back (owner, 10/6/26: "replied ones look the
        // same as the ones not replied to") — at 0.7, so their words still
        // read (the readability floor).
        .opacity(review.isHandled ? 0.7 : 1)
        // One element with a sentence, rather than six unlabelled pieces.
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilitySummary)
        .accessibilityHint("Opens the review and its reply")
    }

    /// The drafted reply a row previews: drafted, with words, and not
    /// flagged for a read (a flagged draft is read on its own screen).
    static func draftPreview(_ review: Review) -> String? {
        guard review.responseStatus == "drafted", !review.draftIsFlagged else { return nil }
        let draft = (review.draftResponse ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return draft.isEmpty ? nil : draft
    }

    private func severityChip(_ label: String) -> some View {
        let tone = review.severity == "safety" ? Color.cavnarRedText : Color.cavnarAmber
        return Button {
            Haptic.selection()
            showingSeverityReason = true
        } label: {
            Text(label)
                .cavnarText(.tag, color: tone)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(tone.opacity(0.13), in: Capsule())
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(review.severityReason == nil)
        .popover(isPresented: $showingSeverityReason) {
            Text(review.severityReason ?? label)
                .cavnarText(.secondary, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 280, alignment: .leading)
                .padding(14)
                .presentationCompactAdaptation(.popover)
        }
    }

    private var accessibilitySummary: String {
        var parts: [String] = []
        parts.append("\(review.rating ?? 0) star review")
        parts.append("by \(review.author ?? "Anonymous")")
        if review.isUrgent && !review.isHandled { parts.append("urgent") }
        if review.isHighSeverity, let label = review.severityLabel {
            parts.append(review.severityReason ?? label)
        }
        parts.append(review.statusPill?.label ?? StatusPill.spokenStatus(review.responseStatus))
        if !review.isAnalysed { parts.append("analysis pending") }
        if let complaint = review.specificComplaint, !complaint.isEmpty { parts.append(complaint) }
        if let date = review.formattedDate { parts.append(date) }
        if let text = review.text, !text.isEmpty { parts.append(text) }
        if let draft = Self.draftPreview(review) { parts.append("Drafted reply: \(draft)") }
        return parts.joined(separator: ", ")
    }
}

struct StarRatingView: View {
    let rating: Int
    /// 12 in a list row (readability round: 10 was under the floor).
    var size: CGFloat = 12
    // Review Detail only: the filled stars light up left to right on
    // first appearance, each with a brief amber glow. Off in list rows,
    // where twenty of them animating at once would just be noise.
    var animated: Bool = false

    @State private var lit = false

    var body: some View {
        stars
            // VoiceOver read "star fill, star fill, star fill, star, star".
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(rating) out of 5 stars")
    }

    private var stars: some View {
        HStack(spacing: animated ? 2 : 1) {
            ForEach(0..<5, id: \.self) { index in
                let filled = index < rating
                Image(systemName: filled ? "star.fill" : "star")
                    .font(.system(size: size))
                    .foregroundStyle(filled ? Color.cavnarAmber : Color.cavnarPaper3)
                    .shadow(color: Color.cavnarAmber.opacity(animated && filled ? 0.55 : 0), radius: 4)
                    .scaleEffect(animated && filled ? (lit ? 1 : 0.35) : 1)
                    .opacity(animated ? (lit ? 1 : 0) : 1)
                    .animation(
                        animated ? .easeOut(duration: 0.34).delay(Double(index) * 0.08) : nil,
                        value: lit
                    )
            }
        }
        .onAppear {
            if animated { lit = true }
        }
    }
}

/// A review's handled state, as the web card draws it (`.rv2-state`,
/// 10/6/26 — 6de5a675): small uppercase words in the state's own colour
/// inside a hairline capsule of the same colour. good = live or replied,
/// warn = approved but not on the platform yet, bad = couldn't post, mute
/// (ink3) = skipped. A review still waiting carries no pill (Review.statusPill).
struct StatusPill: View {
    let label: String
    let tone: CavnarTone

    init(label: String, tone: CavnarTone) {
        self.label = label
        self.tone = tone
    }

    /// The pill for a review as it stands, or nothing while it waits.
    init?(review: Review) {
        guard let pill = review.statusPill else { return nil }
        self.init(label: pill.label, tone: pill.tone)
    }

    var body: some View {
        Text(label)
            .cavnarText(.tag, color: color)
            .lineLimit(1)
            .padding(.horizontal, 9)
            .padding(.vertical, 3)
            .foregroundStyle(color)
            .overlay(Capsule().strokeBorder(color, lineWidth: 1))
    }

    /// The web's tones: --hb-good, --hb-warn, --hb-bad and --ink3.
    private var color: Color {
        switch tone {
        case .good: return .cavnarGreen
        case .warning: return .cavnarAmber
        case .bad: return .cavnarRedText
        case .neutral: return .cavnarInk3
        }
    }

    /// Spoken form for VoiceOver when a review carries no pill.
    static func spokenStatus(_ status: String) -> String {
        switch status {
        case "posted": return "live on the platform"
        case "approved": return "approved, not yet posted"
        case "drafted": return "reply drafted, waiting for you"
        case "skipped": return "skipped"
        default: return "no reply yet"
        }
    }
}
