import SwiftUI

struct ReviewDetailView: View {
    @State private var viewModel: ReviewDetailViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var showingTemplates = false
    @State private var showingRetractConfirm = false
    @State private var showingDeleteConfirm = false
    @State private var postedOverlayLabel: String?
    @FocusState private var isDraftFocused: Bool
    var onCompleted: (String) -> Void
    /// Queue mode (friction audit #21): the next reply waiting after this
    /// one, from the list that opened it, and how that list is told this one
    /// was answered when the screen moves on instead of popping.
    var nextInQueue: ((Int) -> Review?)? = nil
    var onAdvanced: ((String, Int) -> Void)? = nil
    /// The short check between one reply and the next.
    @State private var quickCheckLabel: String?

    init(viewModel: ReviewDetailViewModel, onCompleted: @escaping (String) -> Void,
         nextInQueue: ((Int) -> Review?)? = nil, onAdvanced: ((String, Int) -> Void)? = nil) {
        _viewModel = State(initialValue: viewModel)
        self.onCompleted = onCompleted
        self.nextInQueue = nextInQueue
        self.onAdvanced = onAdvanced
    }

    /// Still waiting on the owner — the pinned bar's Skip / Approve apply.
    private var isActive: Bool {
        !["posted", "approved", "skipped"].contains(viewModel.currentStatus)
    }

    /// The reply "Approve & next" would move on to, if any.
    private var nextReview: Review? { nextInQueue?(viewModel.review.id) }

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
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarRed)
                }
                actionButtons
            }
            .padding(20)
        }
        // A fresh scroll for each reply queue mode moves on to.
        .id(viewModel.review.id)
        // Skip / Approve pinned where the thumb is (friction audit #21) —
        // they sat at the end of the scroll, under the review and the draft.
        .safeAreaInset(edge: .bottom) {
            if isActive {
                VStack(spacing: 0) {
                    Rectangle().fill(Color.cavnarPaper3).frame(height: 1)
                    activeButtons
                        .padding(.horizontal, 20)
                        .padding(.top, 12)
                        .padding(.bottom, 10)
                }
                .background(Color.cavnarPaper.opacity(0.94))
            }
        }
        .overlay(alignment: .bottom) {
            if let label = quickCheckLabel {
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(Color.cavnarGreen)
                    Text(label)
                        .font(.cavnarBody(14.5, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 11)
                .background(Color.cavnarPaper2, in: Capsule())
                .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .padding(.bottom, 96)
                .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .animation(.easeOut(duration: 0.2), value: quickCheckLabel)
        .cavnarModuleBackground()
        .navigationTitle(reviewTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar(reviewTitle) }
        .toolbar {
            cavnarToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button(role: .destructive) {
                        showingDeleteConfirm = true
                    } label: {
                        Label("Delete review", systemImage: "trash")
                    }
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber)
                        .cavnarToolbarIconGlass()
                }
                .tint(nil)
            }
        }
        .confirmationDialog(
            "Delete this review?",
            isPresented: $showingDeleteConfirm,
            titleVisibility: .visible
        ) {
            Button("Delete review", role: .destructive) {
                Task {
                    if await viewModel.deleteReview() {
                        onCompleted("deleted")
                        dismiss()
                    }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("It leaves your inbox and your stats. This doesn't touch the review on \(viewModel.review.platformDisplayName).")
        }
        .cavnarEmberBackButton()
        .keyboardDoneToolbar { isDraftFocused = false }
        .onChange(of: viewModel.didComplete) { _, completed in
            guard completed, let status = viewModel.finalStatus else { return }
            // Queue mode: a short check, then the next reply waiting — the
            // full 1.6s posted moment plays only on the last one (#21).
            if status == "posted" || status == "approved", let next = nextReview {
                let answered = viewModel.review.id
                let label = status == "posted"
                    ? "Posted to \(viewModel.review.platformDisplayName)" : "Approved"
                quickCheckLabel = label
                Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(450))
                    onAdvanced?(status, answered)
                    quickCheckLabel = nil
                    viewModel = ReviewDetailViewModel(review: next)
                    await viewModel.loadTemplates()
                }
                return
            }
            if status == "posted" || status == "approved" {
                // "Posted" — the ember leaves the draft, travels the wire,
                // and lands as a checkmark before this screen goes away.
                // Only ever on the real 200 (didComplete), never optimistic.
                postedOverlayLabel = status == "posted"
                    ? "Reply posted to \(viewModel.review.platformDisplayName)"
                    : "Reply approved"
            } else {
                onCompleted(status)
                dismiss()
            }
        }
        .overlay {
            if let label = postedOverlayLabel {
                ZStack {
                    Color.cavnarPaper.opacity(0.84).ignoresSafeArea()
                    CavnarPostedCheck(label: label) {
                        if let status = viewModel.finalStatus { onCompleted(status) }
                        dismiss()
                    }
                    .padding(.horizontal, 26)
                    .padding(.vertical, 24)
                    .background(Color.cavnarPaper2)
                    .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
                }
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.25), value: postedOverlayLabel != nil)
        .task {
            await viewModel.loadTemplates()
        }

        .sheet(isPresented: $showingTemplates) {
            TemplatePickerSheet(templates: viewModel.templates) { template in
                viewModel.applyTemplate(template)
                showingTemplates = false
            }
        }
        .confirmationDialog(
            "Post this reply anyway?",
            isPresented: $viewModel.needsFlagConfirm,
            titleVisibility: .visible
        ) {
            Button("Post it anyway") {
                Task { await viewModel.approve(confirmFlagged: true) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This reply \(viewModel.flagReason ?? ReviewDetailViewModel.defaultFlagReason). Cavnar AI can\u{2019}t confirm that.")
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
                    .font(.cavnarBody(17.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                HStack(spacing: 6) {
                    StarRatingView(rating: viewModel.review.rating ?? 0, size: 13, animated: true)
                    Text(viewModel.review.platformDisplayName)
                        .font(.cavnarBody(14.5, weight: 700))
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
        VStack(alignment: .leading, spacing: 12) {
            Text(viewModel.review.text ?? "")
                .font(.cavnarBody(17))
                .foregroundStyle(Color.cavnarInk2)
                .lineSpacing(6)
            cavnarRead
            // The web's "Ask about this" on each review card, with the same
            // question and the review as the screen's subject.
            HomeAskLink(
                question: "About this review: what is the guest really saying, and is my reply right?",
                screen: AskScreen(panel: "reviews", entityType: "review", entityId: "\(viewModel.review.id)")
            )
        }
    }

    /// Cavnar's own read of this one review: the one-line summary the
    /// analyser has always written, the concrete thing that went wrong, and
    /// the dish / role / daypart the guest named.
    ///
    /// The summary existed on every review in the database and was rendered
    /// by nothing on either platform. The rest is new, and it is what makes a
    /// dish-level or shift-level pattern possible at all downstream — showing
    /// it here is also the only way the owner can tell whether the extraction
    /// read their review correctly.
    @ViewBuilder
    private var cavnarRead: some View {
        let r = viewModel.review
        let chips = r.entities?.chips ?? []
        if r.summary?.isEmpty == false || r.specificComplaint?.isEmpty == false || !chips.isEmpty {
            VStack(alignment: .leading, spacing: 7) {
                if let summary = r.summary, !summary.isEmpty {
                    Text(summary)
                        .font(.cavnarBody(14))
                        .italic()
                        .foregroundStyle(Color.cavnarInk3)
                        .lineSpacing(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !chips.isEmpty || r.specificComplaint?.isEmpty == false {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 5) {
                            if let what = r.specificComplaint, !what.isEmpty {
                                Text(what)
                                    .font(.cavnarBody(11.5, weight: 600))
                                    .foregroundStyle(Color.cavnarInk2)
                                    .padding(.horizontal, 9).padding(.vertical, 3)
                                    .background(Color.cavnarEmber.opacity(0.12), in: Capsule())
                            }
                            ForEach(chips, id: \.self) { chip in
                                Text(chip)
                                    .font(.cavnarBody(11.5))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .padding(.horizontal, 9).padding(.vertical, 3)
                                    .overlay(Capsule().stroke(Color.cavnarInk3.opacity(0.28), lineWidth: 1))
                            }
                        }
                        .padding(.vertical, 1)
                    }
                    .scrollBounceBehavior(.basedOnSize)
                }
            }
            .padding(.leading, 11)
            .overlay(alignment: .leading) {
                Rectangle().fill(Color.cavnarEmber.opacity(0.35)).frame(width: 2)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Cavnar AI's read. \(r.summary ?? "")"
                                + (r.specificComplaint.map { " Issue: \($0)." } ?? "")
                                + (chips.isEmpty ? "" : " Mentioned: \(chips.joined(separator: ", "))."))
        }
    }

    // A review reopened after being acted on (posted/approved/skipped) is
    // final — editing or regenerating its draft wouldn't do anything
    // meaningful, so those controls only show while it's still actionable.
    private var isFinal: Bool {
        ["posted", "approved", "skipped"].contains(viewModel.currentStatus)
    }

    private var draftEditor: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text("AI-drafted response")
                    .font(.cavnarBody(14.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                if !isFinal {
                    // Same at-rest "this is yours to change" cue Account's
                    // editable fields use (AccountFieldLabel) — nothing else
                    // here told a first-time user the draft below is
                    // actually editable text, not a locked preview.
                    Image(systemName: "pencil")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber.opacity(0.7))
                }
                Spacer()
                if !isFinal {
                    if !viewModel.templates.isEmpty {
                        Button {
                            Haptic.light()
                            showingTemplates = true
                        } label: {
                            Label("Templates", systemImage: "doc.on.doc")
                                .font(.cavnarBody(14, weight: 600))
                        }
                    }
                    Button {
                        Haptic.light()
                        Task { await viewModel.regenerateDraft() }
                    } label: {
                        Label("Regenerate", systemImage: "arrow.clockwise")
                            .font(.cavnarBody(14, weight: 600))
                    }
                    .disabled(viewModel.isSubmitting)
                }
            }
            // A draft that generated cleanly but states a specific action the
            // restaurant may never have taken. The 1-star prompt asks the
            // model to "explain what will be done differently", so an
            // invented remediation — staff retrained, supplier changed —
            // used to go out as a statement of fact under the owner's name.
            // It follows the text on screen: a regenerate or a save that
            // clears or raises the flag updates it here, as on the web.
            if let reason = viewModel.flagReason, !isFinal {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.cavnarAmber)
                        .padding(.top, 1)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Read this one before you post it")
                            .font(.cavnarBody(14, weight: 700))
                            .foregroundStyle(Color.cavnarAmber)
                        Text(reason)
                            .font(.cavnarBody(13.5))
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(11)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(Color.cavnarAmber.opacity(0.12))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .stroke(Color.cavnarAmber.opacity(0.35), lineWidth: 1)
                )
            }
            if viewModel.needsDraft && !viewModel.isGeneratingDraft {
                // Explicit, like the web's "Draft a reply". Opening a review
                // no longer spends a Sonnet call on its own.
                VStack(alignment: .leading, spacing: 10) {
                    Text("No reply drafted yet")
                        .font(.cavnarBody(13, weight: 700))
                        .tracking(0.6)
                        .textCase(.uppercase)
                        .foregroundStyle(Color.cavnarEmber)
                    Button {
                        Haptic.light()
                        Task { await viewModel.regenerateDraft() }
                    } label: {
                        Label("Write a reply with Cavnar AI", systemImage: "sparkles")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarGlassButtonStyle(isProminent: true, isDisabled: viewModel.isSubmitting))
                    // Disabled, not just dimmed: each tap is a paid draft
                    // (CLIENT-55).
                    .disabled(viewModel.isSubmitting)
                }
                .padding(14)
                .background(Color.cavnarEmber.opacity(0.12))
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            } else if viewModel.isGeneratingDraft {
                // "Composing" — an ember caret writing each line into place
                // while Claude drafts the reply (see CavnarMotion). Covers
                // the very first auto-draft AND a manual Regenerate tap —
                // both route through regenerateDraft().
                CavnarComposingLines(widths: [1.0, 0.95, 0.9, 0.97, 0.8, 0.88, 0.5], lineHeight: 12, spacing: 10)
                    .frame(minHeight: 130)
                    .padding(14)
                    .background(Color.cavnarEmber.opacity(0.20))
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            } else {
                // TextEditor (was here originally) is backed by a full
                // UITextView — with this app's custom Apfel Grotezk font,
                // that first becomeFirstResponder/layout pass on tapping in
                // has to build a glyph cache for a font UIKit hasn't
                // measured before, which is a well-known TextEditor+custom-
                // font stutter, and it recurs every time since this view
                // (and its TextEditor) is recreated fresh per review. Same
                // fix Account's editable fields already use for a related
                // TextEditor sizing bug (see AccountFieldRow's doc comment):
                // TextField(_:text:axis:) is a lighter-weight growing-field
                // primitive, not TextEditor's full rich-text stack, and
                // doesn't carry the same first-focus cost.
                TextField("", text: $viewModel.editedDraft, axis: .vertical)
                    .font(.cavnarBody(17))
                    .lineSpacing(5)
                    .lineLimit(6...20)
                    .focused($isDraftFocused)
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
                // Blue for live-on-the-platform, green for approved —
                // matching the web, which had them the other way round here.
                completedBanner(
                    "Posted", icon: "checkmark.circle.fill", color: .cavnarBlue, background: .cavnarBlueBg,
                    // Retract only where it can actually work: posted AND
                    // google AND a review_name our own auto-post set
                    // (server-computed can_retract). It was offered on every
                    // posted review, including Yelp ones, and every one of
                    // those failed with "only supported for auto-posted
                    // Google replies".
                    undoLabel: viewModel.review.canRetract ? "Retract from Google" : nil,
                    isDestructiveUndo: true
                ) {
                    showingRetractConfirm = true
                }
            case "approved":
                VStack(spacing: 10) {
                    // The reply was approved but Google refused it. The
                    // post runs synchronously server-side, so this is a
                    // finished failure, not work still in flight — it used
                    // to show a plain "Approved" with no sign anything had
                    // gone wrong and no way to try again.
                    if let failure = viewModel.postFailure {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("Couldn't post to Google", systemImage: "exclamationmark.triangle.fill")
                                .font(.cavnarBody(14.5, weight: 700))
                                .foregroundStyle(Color.cavnarRed)
                            Text(failure)
                                .font(.cavnarBody(13))
                                .foregroundStyle(Color.cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                            Button {
                                Haptic.light()
                                Task {
                                    if await viewModel.retryPost() {
                                        onCompleted(viewModel.currentStatus)
                                    }
                                }
                            } label: {
                                Label("Retry posting", systemImage: "arrow.clockwise")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarGlassButtonStyle(isProminent: true, isDisabled: viewModel.isSubmitting))
                            // Each tap posts to Google (CLIENT-55).
                            .disabled(viewModel.isSubmitting)
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.cavnarRedBg)
                        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                        .accessibilityElement(children: .contain)
                    }
                    completedBanner(
                        "Approved", icon: "checkmark.circle.fill", color: .cavnarGreen, background: .cavnarGreenBg,
                        undoLabel: "Undo"
                    ) {
                        Task {
                            if await viewModel.undo() {
                                onCompleted(viewModel.currentStatus)
                            }
                        }
                    }
                    // Google replies post themselves once GBP is connected;
                    // everywhere else the owner pastes the reply in by hand
                    // and tells Cavnar it's live — same as the web's button.
                    if viewModel.review.platform != "google", !viewModel.isSubmitting {
                        Button {
                            Task {
                                if await viewModel.markPosted() {
                                    onCompleted(viewModel.currentStatus)
                                }
                            }
                        } label: {
                            Label("Mark as posted on \(viewModel.review.platformDisplayName)", systemImage: "checkmark.seal")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
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
                // Skip / Approve are pinned to the bottom (safeAreaInset).
                EmptyView()
            }
        }
    }

    /// undoLabel nil hides the undo action entirely — used where the
    /// action exists in principle but cannot succeed for this review (a
    /// Retract with no Google reply of ours behind it).
    private func completedBanner(
        _ text: String, icon: String, color: Color, background: Color,
        undoLabel: String?, isDestructiveUndo: Bool = false,
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
                CavnarWorkingLine(width: 80)
            } else if let undoLabel {
                Button(undoLabel, role: isDestructiveUndo ? .destructive : nil) {
                    undo()
                }
                .font(.cavnarBody(14.5, weight: 600))
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
                // isApproving, not isSubmitting — regenerate/skip/save also
                // set isSubmitting, and none of those are "posting."
                if viewModel.isApproving {
                    CavnarShimmerText(text: "Posting…", color: Color.cavnarInk)
                } else if nextReview != nil {
                    // Posts this one, then opens the next reply waiting.
                    Text("Approve & next")
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
                        Text(template.title).font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Text(template.body).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3).lineLimit(2)
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .cavnarModuleBackground()
            .navigationTitle("Response Templates")
            .toolbar { cavnarTitleToolbar("Response Templates") }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}
