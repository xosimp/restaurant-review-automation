import SwiftUI

struct ReviewDetailView: View {
    @State private var viewModel: ReviewDetailViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var showingTemplates = false
    @State private var showingRetractConfirm = false
    var onCompleted: (String) -> Void

    init(viewModel: ReviewDetailViewModel, onCompleted: @escaping (String) -> Void) {
        _viewModel = State(initialValue: viewModel)
        self.onCompleted = onCompleted
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
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
        .cavnarModuleBackground()
        .navigationTitle(reviewTitle)
        .navigationBarTitleDisplayMode(.inline)
        .cavnarEmberTitle(reviewTitle)
        .cavnarEmberBackButton()
        .onChange(of: viewModel.didComplete) { _, completed in
            if completed, let status = viewModel.finalStatus {
                onCompleted(status)
                dismiss()
            }
        }
        .task {
            await viewModel.loadTemplates()
        }
        .task {
            await viewModel.ensureDraftIfNeeded()
        }
        .sheet(isPresented: $showingTemplates) {
            TemplatePickerSheet(templates: viewModel.templates) { template in
                viewModel.applyTemplate(template)
                showingTemplates = false
            }
        }
        .confirmationDialog(
            "Retract this reply from Google?",
            isPresented: $showingRetractConfirm,
            titleVisibility: .visible
        ) {
            Button("Retract Reply", role: .destructive) {
                Task {
                    if await viewModel.retract() {
                        onCompleted(viewModel.currentStatus)
                    }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This reply is currently live on your Google Business Profile. Retracting removes it from Google immediately.")
        }
    }

    private var reviewTitle: String {
        guard let author = viewModel.review.author, let firstName = author.split(separator: " ").first else {
            return "Review"
        }
        return "\(firstName)'s Review"
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 7) {
                Text(viewModel.review.author ?? "Anonymous")
                    .font(.cavnarBody(16, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                HStack(spacing: 6) {
                    StarRatingView(rating: viewModel.review.rating ?? 0)
                    Text(viewModel.review.platformDisplayName)
                        .font(.cavnarBody(11, weight: 700))
                        .tracking(0.4)
                        .textCase(.uppercase)
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer()
            StatusPill(status: viewModel.currentStatus)
        }
    }

    private var reviewText: some View {
        Text(viewModel.review.text ?? "")
            .font(.cavnarBody(15))
            .foregroundStyle(Color.cavnarInk2)
            .lineSpacing(6)
    }

    // A review reopened after being acted on (posted/approved/skipped) is
    // final — editing or regenerating its draft wouldn't do anything
    // meaningful, so those controls only show while it's still actionable.
    private var isFinal: Bool {
        ["posted", "approved", "skipped"].contains(viewModel.currentStatus)
    }

    private var draftEditor: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("AI-drafted response")
                    .font(.cavnarBody(12, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer()
                if !isFinal {
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
            }
            if viewModel.isLoadingInitialDraft {
                CavnarSkeletonLines(widths: [1.0, 0.86, 0.55], lineHeight: 12, spacing: 10)
                    .frame(minHeight: 130)
                    .padding(14)
                    .background(Color.cavnarEmber.opacity(0.20))
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            } else {
                TextEditor(text: $viewModel.editedDraft)
                    .font(.cavnarBody(15))
                    .lineSpacing(5)
                    // TextEditor keeps its own opaque system background by
                    // default, which was rendering as a second, darker box
                    // nested inside this one — .scrollContentBackground
                    // hides it so only our own fill shows.
                    .scrollContentBackground(.hidden)
                    // Also fixes an inconsistent-height bug: without this,
                    // TextEditor sometimes sized itself to fit all the text
                    // and sometimes clipped it to just past minHeight,
                    // depending on whether the text arrived before or after
                    // the first layout pass. Disabling its own internal
                    // scrolling forces it to size to its content instead,
                    // deferring scrolling to the outer ScrollView.
                    .scrollDisabled(true)
                    .frame(minHeight: 130)
                    .padding(14)
                    .background(Color.cavnarEmber.opacity(0.20))
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .disabled(isFinal)
                    .onChange(of: viewModel.editedDraft) { _, _ in
                        viewModel.scheduleDraftSave()
                    }
            }
        }
    }

    private var actionButtons: some View {
        Group {
            switch viewModel.currentStatus {
            case "posted":
                completedBanner(
                    "Posted", icon: "checkmark.circle.fill", color: .cavnarGreen, background: .cavnarGreenBg,
                    undoLabel: "Retract from Google", isDestructiveUndo: true
                ) {
                    showingRetractConfirm = true
                }
            case "approved":
                completedBanner(
                    "Approved", icon: "checkmark.circle.fill", color: .cavnarBlue, background: .cavnarBlueBg,
                    undoLabel: "Undo"
                ) {
                    Task {
                        if await viewModel.undo() {
                            onCompleted(viewModel.currentStatus)
                        }
                    }
                }
            case "skipped":
                completedBanner(
                    "Skipped", icon: "minus.circle.fill", color: .cavnarInk3, background: .cavnarPaper2,
                    undoLabel: "Undo"
                ) {
                    Task {
                        if await viewModel.undo() {
                            onCompleted(viewModel.currentStatus)
                        }
                    }
                }
            default:
                activeButtons
            }
        }
    }

    private func completedBanner(
        _ text: String, icon: String, color: Color, background: Color,
        undoLabel: String, isDestructiveUndo: Bool = false,
        undo: @escaping () -> Void
    ) -> some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: icon)
                Text(text).font(.cavnarBody(15, weight: 700))
            }
            .foregroundStyle(color)
            .frame(maxWidth: .infinity)
            .padding(14)
            .background(background)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))

            if viewModel.isSubmitting {
                ProgressView()
            } else {
                Button(undoLabel, role: isDestructiveUndo ? .destructive : nil) {
                    undo()
                }
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(isDestructiveUndo ? Color.cavnarRed : Color.cavnarInk3)
            }
        }
    }

    private var activeButtons: some View {
        HStack(spacing: 12) {
            Button {
                Task { await viewModel.skip() }
            } label: {
                Text("Skip")
            }
            .buttonStyle(CavnarGlassButtonStyle(isProminent: false, isDisabled: viewModel.isSubmitting))
            .disabled(viewModel.isSubmitting)

            Button {
                Task { await viewModel.approve() }
            } label: {
                if viewModel.isSubmitting {
                    ProgressView().tint(Color.cavnarInk)
                } else {
                    Text("Approve & Post")
                }
            }
            .buttonStyle(CavnarGlassButtonStyle(
                isProminent: true,
                isDisabled: viewModel.isSubmitting || viewModel.editedDraft.isEmpty
            ))
            .disabled(viewModel.isSubmitting || viewModel.editedDraft.isEmpty)
        }
    }
}

/// Loading placeholder for the draft box while the very-first AI draft is
/// being generated — the app's standing convention is a sliding ember pulse
/// bar for any quick async load, not a spinner or blank space. Three
/// paragraph-shaped lines, each with its own sweeping ember shimmer,
/// standing in for the text that's about to arrive.
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
            .scrollContentBackground(.hidden)
            .background(Color.cavnarPaper)
            .navigationTitle("Response Templates")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}
