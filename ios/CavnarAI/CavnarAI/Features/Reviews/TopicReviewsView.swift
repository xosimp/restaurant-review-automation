import SwiftUI

/// Pushed from a topic-sentiment card in ReviewsAnalyticsSection — reuses
/// ReviewsListViewModel/ReviewRow rather than a bespoke list, just scoped to
/// one topic-heatmap category via the /mobile/api/reviews?category= filter.
struct TopicReviewsView: View {
    let category: String
    let label: String

    @State private var viewModel = ReviewsListViewModel()

    var body: some View {
        Group {
            if viewModel.reviews.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                ContentUnavailableView("No \(label) reviews", systemImage: "star.bubble")
            } else {
                List(viewModel.reviews) { review in
                    NavigationLink {
                        ReviewDetailView(
                            viewModel: ReviewDetailViewModel(review: review),
                            onCompleted: { status in viewModel.markCompleted(reviewID: review.id, status: status) }
                        )
                    } label: {
                        ReviewRow(review: review)
                    }
                    .listRowBackground(Color.clear)
                    .listRowSeparatorTint(Color.cavnarPaper3)
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        }
        .overlay {
            if viewModel.isLoading && viewModel.reviews.isEmpty { ProgressView() }
        }
        .refreshable { await viewModel.load(category: category) }
        .cavnarModuleBackground()
        .navigationTitle(label)
        .navigationBarTitleDisplayMode(.inline)
        .cavnarEmberTitle(label)
        .cavnarEmberBackButton()
        .task { await viewModel.load(category: category) }
    }
}
