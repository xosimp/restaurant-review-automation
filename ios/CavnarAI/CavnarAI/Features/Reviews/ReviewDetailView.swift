import SwiftUI

struct ReviewDetailView: View {
    @State private var viewModel: ReviewDetailViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var showingTemplates = false
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
        .task {
            await viewModel.loadTemplates()
        }
        .sheet(isPresented: $showingTemplates) {
            TemplatePickerSheet(templates: viewModel.templates) { template in
                viewModel.applyTemplate(template)
                showingTemplates = false
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
                if !viewModel.templates.isEmpty {
                    Button {
                        showingTemplates = true
                    } label: {
                        Label("Templates", systemImage: "doc.on.doc")
                            .font(.cavnarBody(12, weight: 600))
                    }
                }
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

private struct TemplatePickerSheet: View {
    let templates: [ResponseTemplate]
    let onSelect: (ResponseTemplate) -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List(templates) { template in
                Button {
                    onSelect(template)
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(template.title).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Text(template.body).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3).lineLimit(2)
                    }
                }
            }
            .navigationTitle("Response Templates")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}
