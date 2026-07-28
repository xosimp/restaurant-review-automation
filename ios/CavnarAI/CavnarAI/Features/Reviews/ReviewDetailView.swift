import SwiftUI

struct ReviewDetailView: View {
    @State private var viewModel: ReviewDetailViewModel
    @Environment(\.dismiss) private var dismiss
    var onCompleted: () -> Void

    init(viewModel: ReviewDetailViewModel, onCompleted: @escaping () -> Void) {
        _viewModel = State(initialValue: viewModel)
        self.onCompleted = onCompleted
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                Divider()
                reviewText
                Divider()
                draftEditor
                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                }
                actionButtons
            }
            .padding(20)
        }
        .background(Color.cavnarPaper)
        .navigationTitle("Review")
        .navigationBarTitleDisplayMode(.inline)
        .onChange(of: viewModel.didComplete) { _, completed in
            if completed {
                onCompleted()
                dismiss()
            }
        }
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text(viewModel.review.author ?? "Anonymous")
                    .font(.cavnarBody(16, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                StarRatingView(rating: viewModel.review.rating ?? 0)
            }
            Spacer()
            StatusPill(status: viewModel.review.responseStatus)
        }
    }

    private var reviewText: some View {
        Text(viewModel.review.text ?? "")
            .font(.cavnarBody(14))
            .foregroundStyle(Color.cavnarInk2)
    }

    private var draftEditor: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("AI-drafted response")
                    .font(.cavnarBody(12, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer()
                Button {
                    Task { await viewModel.regenerateDraft() }
                } label: {
                    Label("Regenerate", systemImage: "arrow.clockwise")
                        .font(.cavnarBody(12, weight: 600))
                }
                .disabled(viewModel.isSubmitting)
            }
            TextEditor(text: $viewModel.editedDraft)
                .font(.cavnarBody(14))
                .frame(minHeight: 120)
                .padding(10)
                .background(Color.cavnarPaper2)
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .onChange(of: viewModel.editedDraft) { _, _ in
                    viewModel.scheduleDraftSave()
                }
        }
    }

    private var actionButtons: some View {
        HStack(spacing: 12) {
            Button {
                Task { await viewModel.skip() }
            } label: {
                Text("Skip")
                    .font(.cavnarBody(15, weight: 600))
                    .frame(maxWidth: .infinity)
                    .padding(14)
            }
            .foregroundStyle(Color.cavnarInk3)
            .background(Color.cavnarPaper2)
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .disabled(viewModel.isSubmitting)

            Button {
                Task { await viewModel.approve() }
            } label: {
                if viewModel.isSubmitting {
                    ProgressView().tint(.white).frame(maxWidth: .infinity)
                } else {
                    Text("Approve & Post")
                }
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(viewModel.isSubmitting || viewModel.editedDraft.isEmpty)
        }
    }
}
