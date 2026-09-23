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
        .navigationDestination(item: $deepLinkedReview) { review in
            ReviewDetailView(
                viewModel: ReviewDetailViewModel(review: review),
                onCompleted: { status in viewModel.markCompleted(reviewID: review.id, status: status) }
            )
        }
        .task {
            await viewModel.load()
            openDeepLinkIfNeeded()
        }
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
                                .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 14, trailing: 16))
                        }
                    }
                    Section {
                        inboxFilters
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 6, trailing: 16))
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
                        Haptic.light()
                        deepLinkedReview = review
                    } label: {
                        ReviewRow(review: review)
                            .cavnarRowEntrance(index: index, clock: clock)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
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
                        HStack { Spacer(); ProgressView(); Spacer() }
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
            Task { await viewModel.reload() }
        }
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
        guard let reviewID = deepLinkRouter.consumePendingReviewID(),
              let match = viewModel.reviews.first(where: { $0.id == reviewID }) else {
            return
        }
        deepLinkedReview = match
    }
}

struct ReviewRow: View {
    let review: Review

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                StarRatingView(rating: review.rating ?? 0)
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text(review.author ?? "Anonymous")
                            .font(.cavnarBody(14.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        StatusPill(status: review.responseStatus)
                        if let date = review.formattedDate {
                            Text(date)
                                .font(.cavnarBody(14.5))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        if review.isUrgent {
                            Image(systemName: "exclamationmark.circle.fill")
                                .foregroundStyle(Color.cavnarRed)
                                .font(.system(size: 12))
                        }
                        // The severity tier, for the two tiers an owner must
                        // not scroll past. The urgency dot above answers
                        // "should this have woken me up?"; this answers "what
                        // kind of problem is it?", which is what orders a
                        // dozen open complaints on a Tuesday morning.
                        if review.isHighSeverity, let label = review.severityLabel {
                            Text(label)
                                .font(.cavnarBody(10, weight: 700))
                                .tracking(0.6)
                                .textCase(.uppercase)
                                .foregroundStyle(review.severity == "safety" ? Color.cavnarRed : Color.cavnarAmber)
                                .padding(.horizontal, 7).padding(.vertical, 2)
                                .background((review.severity == "safety" ? Color.cavnarRed : Color.cavnarAmber).opacity(0.13),
                                            in: Capsule())
                        }
                    }
                    Text(review.text ?? "")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineLimit(2)
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
            Image(systemName: "chevron.right")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Color.cavnarInk3)
        }
        .padding(.vertical, 4)
        // One element with a sentence, rather than six unlabelled pieces.
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilitySummary)
        .accessibilityHint("Opens the review and its reply")
    }

    private var accessibilitySummary: String {
        var parts: [String] = []
        parts.append("\(review.rating ?? 0) star review")
        parts.append("by \(review.author ?? "Anonymous")")
        if review.isUrgent { parts.append("urgent") }
        if review.isHighSeverity, let label = review.severityLabel { parts.append(label) }
        parts.append(StatusPill.spokenStatus(review.responseStatus))
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

struct StatusPill: View {
    let status: String

    var body: some View {
        Text(label)
            .font(.cavnarBody(13.5, weight: 700))
            .tracking(0.4)
            .textCase(.uppercase)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(background)
            .foregroundStyle(foreground)
            .clipShape(Capsule())
    }

    private var label: String {
        switch status {
        case "posted": return "Live"
        case "approved": return "Approved"
        case "drafted": return "Pending"
        case "skipped": return "Skipped"
        default: return "New"
        }
    }

    /// Spoken form for VoiceOver — "Live" alone doesn't say live where.
    static func spokenStatus(_ status: String) -> String {
        switch status {
        case "posted": return "live on the platform"
        case "approved": return "approved, not yet posted"
        case "drafted": return "reply drafted, waiting for you"
        case "skipped": return "skipped"
        default: return "no reply yet"
        }
    }

    // Blue = live on the platform, green = approved. This was the other way
    // round here while the web used blue for posted and green for approved,
    // so the same two states were shown in each other's colours depending
    // on which screen the owner happened to be looking at.
    private var background: Color {
        switch status {
        case "posted": return .cavnarBlueBg
        case "approved": return .cavnarGreenBg
        case "drafted": return .cavnarAmberBg
        case "skipped": return .cavnarPaper3
        default: return .cavnarEmber.opacity(0.1)
        }
    }

    private var foreground: Color {
        switch status {
        case "posted": return .cavnarBlue
        case "approved": return .cavnarGreen
        case "drafted": return .cavnarAmber
        case "skipped": return .cavnarInk3
        default: return .cavnarEmber
        }
    }
}
