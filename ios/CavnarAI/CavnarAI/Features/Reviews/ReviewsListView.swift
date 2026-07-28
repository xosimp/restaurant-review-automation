import SwiftUI

struct ReviewsListView: View {
    @State private var viewModel = ReviewsListViewModel()
    @State private var deepLinkedReview: Review?
    /// Set by DeepLinkRouter when a push notification is tapped — auto-opens
    /// that review's detail screen once the list has loaded.
    var deepLinkReviewID: Int?

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.reviews.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                    ContentUnavailableView("No reviews", systemImage: "star.bubble")
                } else {
                    List(viewModel.reviews) { review in
                        NavigationLink {
                            ReviewDetailView(
                                viewModel: ReviewDetailViewModel(review: review),
                                onCompleted: { viewModel.removeFromQueue(reviewID: review.id) }
                            )
                        } label: {
                            ReviewRow(review: review)
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .background(Color.cavnarPaper)
            .overlay {
                if viewModel.isLoading && viewModel.reviews.isEmpty { ProgressView() }
            }
            .refreshable { await viewModel.load() }
            .navigationTitle("Reviews")
            .navigationDestination(item: $deepLinkedReview) { review in
                ReviewDetailView(
                    viewModel: ReviewDetailViewModel(review: review),
                    onCompleted: { viewModel.removeFromQueue(reviewID: review.id) }
                )
            }
            .task {
                await viewModel.load()
                openDeepLinkIfNeeded()
            }
            .onChange(of: deepLinkReviewID) { _, _ in openDeepLinkIfNeeded() }
        }
    }

    private func openDeepLinkIfNeeded() {
        guard let deepLinkReviewID, let match = viewModel.reviews.first(where: { $0.id == deepLinkReviewID }) else {
            return
        }
        deepLinkedReview = match
    }
}

private struct ReviewRow: View {
    let review: Review

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            StarRatingView(rating: review.rating ?? 0)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(review.author ?? "Anonymous")
                        .font(.cavnarBody(13, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    StatusPill(status: review.responseStatus)
                    if review.isUrgent {
                        Image(systemName: "exclamationmark.circle.fill")
                            .foregroundStyle(Color.cavnarRed)
                            .font(.system(size: 12))
                    }
                }
                Text(review.text ?? "")
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, 4)
    }
}

struct StarRatingView: View {
    let rating: Int

    var body: some View {
        HStack(spacing: 1) {
            ForEach(0..<5, id: \.self) { index in
                Image(systemName: index < rating ? "star.fill" : "star")
                    .font(.system(size: 10))
                    .foregroundStyle(index < rating ? Color.cavnarAmber : Color.cavnarPaper3)
            }
        }
    }
}

struct StatusPill: View {
    let status: String

    var body: some View {
        Text(label)
            .font(.cavnarBody(9, weight: 700))
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
        default: return .cavnarEmber.opacity(0.1)
        }
    }

    private var foreground: Color {
        switch status {
        case "posted": return .cavnarGreen
        case "approved": return .cavnarBlue
        default: return .cavnarEmber
        }
    }
}
