import SwiftUI

/// Pushed from a topic-sentiment card or a platform card in
/// ReviewsAnalyticsSection — reuses ReviewsListViewModel/ReviewRow rather
/// than a bespoke list, just scoped to one topic-heatmap category and/or
/// one platform via the /mobile/api/reviews?category=&platform= filters.
struct FilteredReviewsView: View {
    let title: String
    var category: String?
    var platform: String?

    @State private var viewModel = ReviewsListViewModel()

    var body: some View {
        Group {
            if viewModel.reviews.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                CavnarEmptyHearth(
                    title: "No \(title) reviews",
                    message: "Nothing in this category yet."
                )
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
            if viewModel.isLoading && viewModel.reviews.isEmpty { CavnarLoadingSeal() }
        }
        .cavnarEmberRefreshable { await viewModel.load(category: category, platform: platform) }
        .cavnarModuleBackground()
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .cavnarEmberBackButton()
        .task { await viewModel.load(category: category, platform: platform) }
    }
}
