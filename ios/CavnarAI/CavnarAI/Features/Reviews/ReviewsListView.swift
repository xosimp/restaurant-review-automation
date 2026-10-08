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
    /// The row whose swipe-approve failed, with the server's sentence.
    @State private var rowError: (id: Int, message: String)?
    @State private var approvingRowId: Int?
    @State private var postedLabel: String?
    @State private var focusConsumed = false
    /// Select mode (parity audit 10/7/26 #34): a box on every review still
    /// waiting on a decision, and a bar to approve or skip the ticked ones.
    @State private var editMode: EditMode = .inactive
    @State private var confirmingBulkApprove = false
    @State private var confirmingBulkSkip = false
    /// "Publish N ready": Home's publish confirm, from the inbox header.
    @State private var showingPublishReady = false

    private var isSelecting: Bool { editMode.isEditing }

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
            if subTab == .inbox && viewModel.reviews.contains(where: ReviewsListViewModel.isSelectable) {
                cavnarToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.2)) {
                            if isSelecting {
                                editMode = .inactive
                                viewModel.selection.removeAll()
                            } else {
                                editMode = .active
                            }
                        }
                    } label: {
                        Text(isSelecting ? "Done" : "Select")
                            .font(.cavnarBody(15, weight: 600))
                            .foregroundStyle(Color.cavnarEmber)
                            .frame(minHeight: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint(isSelecting ? "Leaves Select mode" : "Choose several reviews to approve or skip")
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
        // The bar's two outward actions ask first, with the counts (the
        // web's confirm): never one tap from a list.
        .confirmationDialog(
            "Approve and post \(viewModel.bulkApprovable.count) \(viewModel.bulkApprovable.count == 1 ? "reply" : "replies")?",
            isPresented: $confirmingBulkApprove, titleVisibility: .visible
        ) {
            Button("Approve \(viewModel.bulkApprovable.count)") {
                Task {
                    await viewModel.bulkApprove()
                    editMode = .inactive
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(Self.bulkApproveMessage(held: viewModel.bulkHeld))
        }
        .confirmationDialog(
            "Skip \(viewModel.bulkSkippable.count) \(viewModel.bulkSkippable.count == 1 ? "review" : "reviews")?",
            isPresented: $confirmingBulkSkip, titleVisibility: .visible
        ) {
            Button("Skip \(viewModel.bulkSkippable.count)", role: .destructive) {
                Task {
                    await viewModel.bulkSkip()
                    editMode = .inactive
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Nothing is posted; each can still be approved or undone from its review.")
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
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
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
                        // not the reviews_live flag (DH4-6). Amber when a
                        // check has been missed.
                        if let fetch = viewModel.fetchLine {
                            ServerStatusCaption(status: fetch)
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 8, trailing: 16))
                        }
                        // The reputation summary. ReviewStats was modelled
                        // in full and then called from nowhere, so the
                        // phone's Reviews tab opened with no rating, no
                        // response rate and no urgent count while the web
                        // showed all four — the "understand my reputation
                        // in ten seconds" test failed here and passed there.
                        if let stats = viewModel.stats {
                            ReviewsStatStrip(stats: stats)
                                .listRowBackground(Color.clear)
                                .listRowSeparator(.hidden)
                                .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 8, trailing: 16))
                            // What came in and how fast it's answered — the
                            // web header's "6 new this month · avg reply 5h".
                            HStack(spacing: 10) {
                                HomeMixedText.make(stats.receivedLine, size: CavnarType.secondary, weight: 500,
                                                   color: .cavnarInk3)
                                Spacer(minLength: 0)
                                if let ready = stats.publishable, ready > 0, !isSelecting {
                                    publishReadyPill(ready)
                                }
                            }
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 12, trailing: 16))
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
                                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                                        HomeMixedText.make(why, size: CavnarType.secondary, weight: 600,
                                                           color: stats.urgent > 0 ? .cavnarRed : .cavnarInk2)
                                            .fixedSize(horizontal: false, vertical: true)
                                        Text(stats.urgent > 0 ? "Open \u{2192}" : "See why \u{2192}")
                                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                            .foregroundStyle(Color.cavnarEmber2)
                                        Spacer(minLength: 0)
                                    }
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                .listRowBackground(Color.clear)
                                .listRowSeparator(.hidden)
                                .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 12, trailing: 16))
                            }
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
                            HomeMixedText.make(note.text, size: 14, weight: 600,
                                               color: note.failed ? .cavnarRed : .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 8)
                            Button {
                                Haptic.light()
                                viewModel.bulkNote = nil
                            } label: {
                                Image(systemName: "xmark")
                                    .font(.system(size: 11, weight: .bold))
                                    .foregroundStyle(Color.cavnarInk3)
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
                                .font(.cavnarBody(14))
                                .foregroundStyle(Color.cavnarRed)
                            Spacer(minLength: 8)
                            Button("Retry") { Task { await viewModel.reload() } }
                                .font(.cavnarBody(14, weight: 600))
                        }
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                    }
                    if viewModel.filteredReviews.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                        Text(viewModel.searchText.isEmpty ? "No \(viewModel.filter.rawValue.lowercased()) reviews" : "Nothing matches \u{201C}\(viewModel.searchText)\u{201D}")
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk3)
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
                        if isSelecting {
                            // Select mode: a tap ticks the review instead of
                            // opening it; answered ones have no box.
                            guard ReviewsListViewModel.isSelectable(review) else { return }
                            Haptic.selection()
                            viewModel.toggleSelection(review)
                            return
                        }
                        Haptic.light()
                        deepLinkedReview = review
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(alignment: .center, spacing: 10) {
                                if isSelecting {
                                    selectionMark(for: review)
                                }
                                ReviewRow(review: review, showsChevron: !isSelecting)
                                    .opacity(approvingRowId == review.id ? 0.5 : 1)
                            }
                            if let rowError, rowError.id == review.id {
                                Text(rowError.message)
                                    .font(.cavnarBody(13))
                                    .foregroundStyle(Color.cavnarRed)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .cavnarRowEntrance(index: index, clock: clock)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    // Swipe to approve a reply that may be published unread
                    // (friction audit #21). A flagged, urgent or old draft
                    // has no swipe: it keeps the read-first rule. Not a FULL
                    // swipe: one flick published to Google with nothing in
                    // between (F3-14) — the button is the decision.
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        if !isSelecting && ReviewsListViewModel.canQuickApprove(review) {
                            Button {
                                quickApprove(review)
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
        .environment(\.editMode, $editMode)
        // Select mode's bar, where the thumb is: what the ticked reviews can
        // become, each behind its own confirm.
        .safeAreaInset(edge: .bottom) {
            if isSelecting {
                bulkBar
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: isSelecting)
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

    /// The tick in Select mode — a filled ember circle when chosen, an empty
    /// ring when it can be, nothing (a spacer) for an answered review.
    @ViewBuilder
    private func selectionMark(for review: Review) -> some View {
        let on = viewModel.selection.contains(review.id)
        if ReviewsListViewModel.isSelectable(review) {
            Image(systemName: on ? "checkmark.circle.fill" : "circle")
                .font(.system(size: 22, weight: .regular))
                .foregroundStyle(on ? Color.cavnarEmber : Color.cavnarInk3)
                .frame(width: 28)
                .accessibilityLabel(on ? "Selected" : "Not selected")
        } else {
            Color.clear.frame(width: 28, height: 22)
        }
    }

    static func bulkApproveMessage(held: Int) -> String {
        "Each goes out as written."
            + (held > 0 ? " The other \(held) stay for you to read one at a time \u{2014} a flagged or urgent reply, or one already decided, is approved on its own." : "")
    }

    /// "3 selected", why some can't go in bulk, then Approve N · Skip N.
    private var bulkBar: some View {
        let n = viewModel.selection.count
        let ap = viewModel.bulkApprovable.count
        let sk = viewModel.bulkSkippable.count
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                HomeMixedText.make(n == 0 ? "Tap the reviews to act on" : "\(n) selected",
                                   size: 15, weight: 700, color: .cavnarInk)
                Spacer(minLength: 0)
                if viewModel.isBulkWorking {
                    CavnarWorkingLine(width: 60)
                }
            }
            if n > 0 {
                HomeMixedText.make(viewModel.bulkHeld > 0
                                   ? "\(viewModel.bulkHeld) \(viewModel.bulkHeld == 1 ? "isn\u{2019}t" : "aren\u{2019}t") ready to post in bulk \u{2014} read \(viewModel.bulkHeld == 1 ? "it" : "them") one at a time."
                                   : "Every selected reply is ready to post.",
                                   size: 13, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    confirmingBulkApprove = true
                } label: {
                    HomeMixedText.make("Approve \(ap)", size: 15, weight: 700, color: .white, numberColor: .white)
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: ap == 0 || viewModel.isBulkWorking))
                .disabled(ap == 0 || viewModel.isBulkWorking)
                Button {
                    Haptic.light()
                    confirmingBulkSkip = true
                } label: {
                    HomeMixedText.make("Skip \(sk)", size: 15, weight: 700, color: .cavnarInk)
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(sk == 0 || viewModel.isBulkWorking)
                .opacity(sk == 0 ? 0.5 : 1)
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 10)
        .background(Color.cavnarPaper2.opacity(0.97))
        .overlay(alignment: .top) { Rectangle().fill(Color.cavnarPaper3).frame(height: 1) }
    }

    /// "Publish 4 ready" — every reply one bulk publish may post, behind
    /// Home's own confirm card (the replies in their own words).
    private func publishReadyPill(_ ready: Int) -> some View {
        Button {
            Haptic.light()
            showingPublishReady = true
        } label: {
            HomeMixedText.make("Publish \(ready) ready", size: 13.5, weight: 700, color: .cavnarEmber2,
                               numberWeight: 700, numberColor: .cavnarEmber2)
                .padding(.horizontal, 14)
                .frame(minHeight: 44)
                .background(Color.cavnarPaper2.opacity(0.85), in: Capsule())
                .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.4), lineWidth: 1))
        }
        .buttonStyle(.plain)
        .accessibilityHint("Shows the replies before anything is posted")
    }

    private var inboxFilters: some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(Color.cavnarInk3)
                TextField("Search reviews", text: Binding(
                    get: { viewModel.searchText }, set: { viewModel.searchText = $0 }))
                    .font(.cavnarBody(16))
                    .autocorrectionDisabled()
                if !viewModel.searchText.isEmpty {
                    Button {
                        Haptic.light()
                        viewModel.searchText = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(Color.cavnarInk3)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(Color.cavnarPaper2)
            .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .clipShape(Capsule())

            ScrollView(.horizontal) {
                HStack(spacing: 8) {
                    ForEach(ReviewInboxFilter.allCases) { f in
                        let on = viewModel.filter == f
                        let n = viewModel.count(for: f)
                        Button {
                            guard !on else { return }
                            Haptic.light()
                            withAnimation(.easeOut(duration: 0.2)) { viewModel.filter = f }
                        } label: {
                            HStack(spacing: 5) {
                                Text(f.rawValue).font(.cavnarBody(14.5, weight: 600))
                                if let n, n > 0 {
                                    Text("\(n)").font(.cavnarNumber(13, weight: 700))
                                        .foregroundStyle(on ? Color.white.opacity(0.85) : Color.cavnarEmber2)
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
    private func quickApprove(_ review: Review) {
        guard approvingRowId == nil else { return }
        approvingRowId = review.id
        rowError = nil
        Task {
            let detail = ReviewDetailViewModel(review: review)
            await detail.approve()
            approvingRowId = nil
            if detail.hasQueuedWrite {
                // Offline: queued behind the banner, honestly labelled.
                viewModel.markCompleted(reviewID: review.id, status: "pending-sync")
            } else if detail.didComplete, let status = detail.finalStatus {
                viewModel.markCompleted(reviewID: review.id, status: status)
                postedLabel = status == "posted" ? "Reply posted to \(review.platformDisplayName)" : "Reply approved"
            } else if detail.needsFlagConfirm {
                // Flagged since the list loaded: the server refused it
                // unread. It is read, and confirmed, on its own screen.
                rowError = (review.id, "Read this one first: it \(detail.flagReason ?? ReviewDetailViewModel.defaultFlagReason). Open it to post.")
            } else if let status = detail.finalStatus, detail.postFailure != nil {
                // Approved but the post failed: the row says so, and the
                // detail screen has the retry. Only a Google refusal is a
                // failed post; not connected yet is "will post once
                // connected" (the server's post_failed rule).
                viewModel.markCompleted(reviewID: review.id,
                                        status: detail.postFailedOnGoogle ? "approved-failed" : status)
                rowError = (review.id, detail.postFailure ?? "Approved, but it didn't post — open it to retry.")
            } else {
                rowError = (review.id, detail.errorMessage ?? "Couldn't approve — open it to try again.")
            }
        }
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
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(review.author ?? "Anonymous")
                            .font(.cavnarBody(14.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        if let date = review.formattedDate {
                            Text(date)
                                .font(.cavnarNumber(14.5))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        // Urgent only while it waits on someone: an
                        // answered review is not urgent (the web card's
                        // `r.urgent and not _handled`, the server's rule).
                        if review.isUrgent && !review.isHandled {
                            Image(systemName: "exclamationmark.circle.fill")
                                .foregroundStyle(Color.cavnarRed)
                                .font(.system(size: 12))
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
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineLimit(review.isHandled ? 3 : 4)
                    // Cavnar's one-line read, which the analyser has written
                    // on every review since the product existed and which
                    // nothing on either platform ever showed.
                    if let complaint = review.specificComplaint, !complaint.isEmpty {
                        Text(complaint)
                            .font(.cavnarBody(11.5, weight: 600))
                            .foregroundStyle(Color.cavnarEmber)
                            .lineLimit(1)
                    }
                    if !review.isAnalysed {
                        // Unanalysed reviews reach the inbox now; saying so
                        // beats showing the "neutral" sentiment they were
                        // never actually assigned.
                        Text("Analysis pending")
                            .font(.cavnarBody(11.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk3)
                            .italic()
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
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(.vertical, 4)
        // Handled reviews sit back (owner, 10/6/26: "replied ones look the
        // same as the ones not replied to") — the web card's 0.6.
        .opacity(review.isHandled ? 0.6 : 1)
        // One element with a sentence, rather than six unlabelled pieces.
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilitySummary)
        .accessibilityHint("Opens the review and its reply")
    }

    private func severityChip(_ label: String) -> some View {
        let tone = review.severity == "safety" ? Color.cavnarRed : Color.cavnarAmber
        return Button {
            Haptic.selection()
            showingSeverityReason = true
        } label: {
            Text(label)
                .font(.cavnarBody(10, weight: 700))
                .tracking(0.6)
                .textCase(.uppercase)
                .foregroundStyle(tone)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(tone.opacity(0.13), in: Capsule())
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(review.severityReason == nil)
        .popover(isPresented: $showingSeverityReason) {
            Text(review.severityReason ?? label)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk)
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
        return parts.joined(separator: ", ")
    }
}

struct StarRatingView: View {
    let rating: Int
    var size: CGFloat = 10
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
            .font(.cavnarBody(10.5, weight: 700))
            .tracking(0.8)
            .textCase(.uppercase)
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
        case .bad: return .cavnarRed
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
