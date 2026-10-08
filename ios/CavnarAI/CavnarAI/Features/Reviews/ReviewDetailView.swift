import SwiftUI

struct ReviewDetailView: View {
    @State private var viewModel: ReviewDetailViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var showingTemplates = false
    @State private var showingRetractConfirm = false
    @State private var showingDeleteConfirm = false
    @State private var showingSaveTemplate = false
    @State private var templateName = ""
    @State private var templateNote: String?
    @State private var postedOverlayLabel: String?
    // "Fix tags" (memory round, 9/29/26): the sheet, and the tags the
    // server answered with once corrected.
    @State private var showingRetag = false
    @State private var retagged: ReviewTags?
    @FocusState private var isDraftFocused: Bool
    @State private var showingSeverityReason = false
    /// Yelp (parity audit 10/7/26 #53): the reply was copied and Yelp for
    /// Business opened; on coming back the owner is asked whether it went up
    /// before anything is marked posted.
    @State private var awaitingYelpReturn = false
    @State private var confirmingYelpPosted = false
    @State private var copiedNote: String?
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.openURL) private var openURL
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
                // Answered outside Cavnar AI: the reply actually posted —
                // never Cavnar AI's unused draft (parity audit 10/7/26 #9).
                if viewModel.isAnsweredElsewhere {
                    elsewhereCard
                } else {
                    draftEditor
                }
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
        // An approve that went through but didn't post keeps the owner here
        // (where Retry is); the list still learns its new state — approved,
        // or "Couldn't post to Google" — instead of showing it waiting.
        .onChange(of: viewModel.finalStatus) { _, status in
            guard status == "approved", !viewModel.didComplete else { return }
            onCompleted(viewModel.listStatus)
        }
        // Back from Yelp: ask, never assume (an outward action's confirm).
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active, awaitingYelpReturn else { return }
            awaitingYelpReturn = false
            confirmingYelpPosted = true
        }
        .confirmationDialog(
            "Did the reply go up on Yelp?",
            isPresented: $confirmingYelpPosted,
            titleVisibility: .visible
        ) {
            Button("Yes, mark it posted") {
                Task {
                    if await viewModel.markPosted() {
                        onCompleted(viewModel.currentStatus)
                    }
                }
            }
            Button("Not yet", role: .cancel) {}
        } message: {
            Text("Cavnar AI can\u{2019}t post to Yelp for you, so it only counts the reply once you say it\u{2019}s live.")
        }

        .sheet(isPresented: $showingRetag) {
            ReviewRetagSheet(review: viewModel.review, current: retagged ?? ReviewTags(viewModel.review)) { tags in
                retagged = tags
            }
        }
        .sheet(isPresented: $showingTemplates) {
            TemplatePickerSheet(templates: viewModel.templates, onSelect: { template in
                viewModel.applyTemplate(template)
                showingTemplates = false
            }, onDelete: { template in
                Task { await viewModel.deleteTemplate(template) }
            })
        }
        .alert("Save as template", isPresented: $showingSaveTemplate) {
            TextField("e.g. Positive 5-star standard", text: $templateName)
            Button("Save template") {
                let name = templateName
                Task { templateNote = await viewModel.saveAsTemplate(title: name) ?? "Template saved" }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Name it so you\u{2019}ll recognise it in Templates.")
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
            Text("This reply \(viewModel.flagReason ?? ReviewDetailViewModel.defaultFlagReason). "
                 + ReviewDetailViewModel.flagFollowUp(viewModel.flagReason))
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
                severityLine
            }
            Spacer()
            // The web card's state pill, as this screen last left it.
            if let pill = viewModel.displayReview.statusPill {
                StatusPill(label: pill.label, tone: pill.tone)
            }
        }
    }

    /// The safety / legal tier, and — on tap — why: this review's own
    /// complaint, then what the tier means (models.severity_reason, the web
    /// chip's hover). Decoded and never shown before.
    @ViewBuilder
    private var severityLine: some View {
        let r = viewModel.review
        if r.isHighSeverity, let label = r.severityLabel {
            let tone = r.severity == "safety" ? Color.cavnarRed : Color.cavnarAmber
            VStack(alignment: .leading, spacing: 6) {
                Button {
                    Haptic.selection()
                    withAnimation(.easeOut(duration: 0.2)) { showingSeverityReason.toggle() }
                } label: {
                    HStack(spacing: 4) {
                        Text(label)
                            .font(.cavnarBody(11, weight: 700))
                            .tracking(0.6)
                            .textCase(.uppercase)
                        if r.severityReason != nil {
                            Image(systemName: showingSeverityReason ? "chevron.up" : "info.circle")
                                .font(.system(size: 10, weight: .bold))
                        }
                    }
                    .foregroundStyle(tone)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(tone.opacity(0.13), in: Capsule())
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(r.severityReason == nil)
                .accessibilityHint(r.severityReason == nil ? "" : "Says why it is rated this serious")
                if showingSeverityReason, let why = r.severityReason {
                    Text(why)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                        .transition(.opacity)
                }
            }
        }
    }

    private var reviewText: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(viewModel.review.text ?? "")
                .font(.cavnarBody(17))
                .foregroundStyle(Color.cavnarInk2)
                .lineSpacing(6)
            cavnarRead
            retagRow
            // The web's "Ask about this" on each review card, with the same
            // question and the review as the screen's subject.
            HomeAskLink(
                question: "About this review: what is the guest really saying, and is my reply right?",
                screen: AskScreen(panel: "reviews", entityType: "review", entityId: "\(viewModel.review.id)")
            )
        }
    }

    /// "Fix tags" — the owner corrects how this review was tagged, and
    /// the tags as corrected once saved (memory round, 9/29/26).
    @ViewBuilder
    private var retagRow: some View {
        if viewModel.review.isAnalysed {
            VStack(alignment: .leading, spacing: 4) {
                if let retagged {
                    HomeMixedText.make(retagged.line, size: 13, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button {
                    Haptic.light()
                    showingRetag = true
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: "tag").font(.system(size: 11, weight: .bold))
                        Text(retagged == nil ? "Fix tags" : "Fix tags again")
                    }
                    .font(.cavnarBody(13.5, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                }
                .buttonStyle(.plain)
                .accessibilityHint("Correct the topics, tone, severity and dishes Cavnar AI tagged")
            }
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
                if !isFinal && !viewModel.editedDraft.isEmpty {
                    HStack(spacing: 10) {
                        Button("Save as template") {
                            Haptic.light()
                            templateName = ""
                            templateNote = nil
                            showingSaveTemplate = true
                        }
                        .font(.cavnarBody(14, weight: 600))
                        .foregroundStyle(Color.cavnarEmber2)
                        if let note = templateNote {
                            Text(note)
                                .font(.cavnarBody(13))
                                .foregroundStyle(note == "Template saved" ? Color.cavnarGreen : Color.cavnarRed)
                        }
                    }
                }
            }
        }
    }

    /// The reply someone posted outside Cavnar AI, read-only: the words
    /// Google gave (when it gave them), who said it was answered and when,
    /// and Undo — the web card's "Replied on Google" box.
    private var elsewhereCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Replied on \(viewModel.review.platformDisplayName)")
                .font(.cavnarBody(13, weight: 700))
                .tracking(0.6)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarEmber)
            if let reply = viewModel.review.externalReply?.trimmingCharacters(in: .whitespacesAndNewlines),
               !reply.isEmpty {
                Text(reply)
                    .font(.cavnarBody(16))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            } else {
                Text("The reply\u{2019}s words weren\u{2019}t sent with the mark \u{2014} it\u{2019}s on \(viewModel.review.platformDisplayName).")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Label("Answered outside Cavnar AI", systemImage: "checkmark.circle.fill")
                    .font(.cavnarBody(13.5, weight: 700))
                    .foregroundStyle(Color.cavnarGreen)
                Spacer(minLength: 0)
            }
            HomeMixedText.make(viewModel.review.externalReplyLine, size: 13, weight: 500, color: .cavnarInk3)
            if viewModel.isSubmitting {
                CavnarWorkingLine(width: 80)
            } else {
                Button {
                    Haptic.light()
                    Task {
                        if await viewModel.undo() {
                            onCompleted(viewModel.currentStatus)
                        }
                    }
                } label: {
                    Text("Undo \u{2014} put it back in my queue")
                        .font(.cavnarBody(14.5, weight: 600))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 44)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarGreen.opacity(0.08))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).strokeBorder(Color.cavnarGreen.opacity(0.25), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private var actionButtons: some View {
        Group {
            switch viewModel.currentStatus {
            case "posted" where viewModel.isAnsweredElsewhere:
                // The reply and its Undo are in the card above (#9).
                EmptyView()
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
                    // Google refused it (the row's post_failed, so this is
                    // here again on reopening). The post runs synchronously
                    // server-side, so this is a finished failure, not work
                    // still in flight.
                    if viewModel.postFailedOnGoogle, let failure = viewModel.postFailure {
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
                                    } else {
                                        onCompleted(viewModel.listStatus)
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
                    approvedNextStep
                }
            case "skipped":
                // Skipped is a decision: the reply stays available, but not
                // as the primary action (the web card, 10/6/26).
                VStack(spacing: 10) {
                    completedBanner(
                        "Skipped \u{00B7} no reply sent", icon: "minus.circle.fill", color: .cavnarInk3,
                        background: .cavnarPaper2, undoLabel: "Undo"
                    ) {
                        Task {
                            if await viewModel.undo() {
                                onCompleted(viewModel.currentStatus)
                            }
                        }
                    }
                    if !viewModel.isSubmitting {
                        HStack(spacing: 10) {
                            Button {
                                Haptic.light()
                                Task { await viewModel.approve() }
                            } label: {
                                Text("\u{2713} Approve after all").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                            .disabled(viewModel.editedDraft.isEmpty)
                            Button {
                                Haptic.light()
                                Task {
                                    await viewModel.regenerateDraft()
                                    // A new draft puts it back in the queue
                                    // (the server's regenerate sets drafted).
                                    if viewModel.errorMessage == nil {
                                        viewModel.currentStatus = "drafted"
                                        onCompleted("drafted")
                                    }
                                }
                            } label: {
                                Label("Regenerate", systemImage: "arrow.clockwise").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                    }
                }
            default:
                // Skip / Approve are pinned to the bottom (safeAreaInset).
                EmptyView()
            }
        }
    }

    /// Under an approved reply that isn't live: what happens next. Google
    /// posts itself once the Business Profile is connected; Yelp is copied
    /// and posted by hand (#53); anywhere else is marked posted by hand —
    /// the web card's three lines.
    @ViewBuilder
    private var approvedNextStep: some View {
        let r = viewModel.review
        if r.platform == "google" {
            if !viewModel.postFailedOnGoogle {
                Label("Will post to Google once connected", systemImage: "clock")
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else if !viewModel.isSubmitting {
            VStack(alignment: .leading, spacing: 8) {
                Text("Copy and post to \(r.platformDisplayName)")
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
                if r.platform == "yelp" {
                    Button {
                        copyAndOpenYelp()
                    } label: {
                        Label("Copy & open Yelp", systemImage: "doc.on.doc")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.editedDraft.isEmpty))
                    .disabled(viewModel.editedDraft.isEmpty)
                    if let copiedNote {
                        Text(copiedNote)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button {
                        Haptic.light()
                        confirmingYelpPosted = true
                    } label: {
                        Text("I posted it on Yelp").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                } else {
                    Button {
                        Task {
                            if await viewModel.markPosted() {
                                onCompleted(viewModel.currentStatus)
                            }
                        }
                    } label: {
                        Label("Mark as posted on \(r.platformDisplayName)", systemImage: "checkmark.seal")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
            }
        }
    }

    /// The web's "Copy & open Yelp": the reply on the clipboard, Yelp for
    /// Business opened to paste it. Nothing is marked posted until the owner
    /// comes back and says it went up.
    private func copyAndOpenYelp() {
        let reply = viewModel.editedDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !reply.isEmpty else { return }
        UIPasteboard.general.string = reply
        Haptic.success()
        copiedNote = "Reply copied \u{2014} paste it on Yelp, then come back."
        awaitingYelpReturn = true
        openURL(ReviewDetailView.yelpForBusinessURL)
    }

    /// Yelp for Business, where an owner answers a review (the web opens
    /// the same address).
    static let yelpForBusinessURL = URL(string: "https://business.yelp.com")!

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
        VStack(spacing: 8) {
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
        // Someone already answered it on Google by hand (10/2/26): out of
        // the queue and the reminders, nothing posted.
        Button {
            Task { await viewModel.markRepliedElsewhere() }
        } label: {
            Text("Mark as replied on \(viewModel.review.platformDisplayName)")
                .font(.cavnarBody(14.5, weight: 600))
                .foregroundStyle(Color.cavnarInk3)
        }
        .disabled(viewModel.isSubmitting)
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
    var onDelete: ((ResponseTemplate) -> Void)? = nil
    @Environment(\.dismiss) private var dismiss
    /// Deleted here, so the row leaves at once (the parent's list is a copy).
    @State private var removed: Set<Int> = []

    var body: some View {
        NavigationStack {
            List(templates.filter { !removed.contains($0.id) }) { template in
                Button {
                    onSelect(template)
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(template.title).font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Text(template.body).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3).lineLimit(2)
                    }
                }
                .swipeActions(edge: .trailing) {
                    if let onDelete {
                        Button(role: .destructive) {
                            removed.insert(template.id)
                            onDelete(template)
                        } label: {
                            Label("Delete", systemImage: "trash")
                        }
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
