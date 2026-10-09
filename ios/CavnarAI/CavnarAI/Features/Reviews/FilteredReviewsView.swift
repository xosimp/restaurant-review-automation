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
            if viewModel.reviews.isEmpty && !viewModel.isLoading, let error = viewModel.errorMessage {
                // A failed load is not an empty list (re-audit 10/8/26 M5):
                // it rendered a blank List with nothing to retry.
                VStack(spacing: CavnarSpace.s) {
                    Text(error)
                        .cavnarText(.secondary)
                        .multilineTextAlignment(.center)
                    Button {
                        Haptic.light()
                        Task { await viewModel.load(category: category, platform: platform) }
                    } label: {
                        Text("Retry").frame(minWidth: 120)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                .padding(.horizontal, CavnarSpace.xl)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if viewModel.reviews.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
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
                        // The NavigationLink draws its own chevron.
                        ReviewRow(review: review, showsChevron: false)
                    }
                    .listRowBackground(Color.clear)
                    .listRowSeparatorTint(Color.cavnarPaper3)
                    // Paged like the main inbox — a busy category on a
                    // restaurant with years of history is not one response.
                    .task {
                        if review.id == viewModel.reviews.last?.id { await viewModel.loadMore() }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        }
        .overlay {
            if viewModel.isLoading && viewModel.reviews.isEmpty { CavnarLoadingOrb() }
        }
        .cavnarEmberRefreshable { await viewModel.load(category: category, platform: platform) }
        .cavnarModuleBackground()
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar(title) }
        .cavnarEmberBackButton()
        .task { await viewModel.load(category: category, platform: platform) }
    }
}
