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
        .task {
            await analyticsViewModel.load()
        }
    }

    @ViewBuilder
    private var inboxContent: some View {
        Group {
            if viewModel.reviews.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                CavnarEmptyHearth(
                    title: "No reviews yet",
                    message: "New reviews land here automatically once your platforms are connected."
                )
            } else {
                List(Array(viewModel.reviews.enumerated()), id: \.element.id) { index, review in
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
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        }
        .overlay {
            if viewModel.isLoading && viewModel.reviews.isEmpty { CavnarLoadingSeal() }
        }
        .cavnarEmberRefreshable { await viewModel.load() }
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
                    }
                    Text(review.text ?? "")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineLimit(2)
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

    private var background: Color {
        switch status {
        case "posted": return .cavnarGreenBg
        case "approved": return .cavnarBlueBg
        case "drafted": return .cavnarAmberBg
        case "skipped": return .cavnarPaper3
        default: return .cavnarEmber.opacity(0.1)
        }
    }

    private var foreground: Color {
        switch status {
        case "posted": return .cavnarGreen
        case "approved": return .cavnarBlue
        case "drafted": return .cavnarAmber
        case "skipped": return .cavnarInk3
        default: return .cavnarEmber
        }
    }
}
