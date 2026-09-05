import SwiftUI
import UIKit

/// Scroll target used while Cavnar's answer is typing out — see the
/// comment at its scrollTo call site in AskCavnarView.body.
private let chatScrollBottomID = "chat-scroll-bottom"

/// Holds the reveal-driven scroll throttle's clock OUTSIDE SwiftUI's
/// dependency tracking. As an `@State var Date` this was written up to
/// 10x/sec during every answer's reveal, and each write invalidated the
/// whole tab's body — header, orb, input bar, every bubble — for a value
/// nothing on screen even reads. A reference type's property changing
/// invalidates nothing.
@MainActor
private final class ScrollThrottle {
    var last = Date.distantPast
}

/// The Ask Cavnar tab. Owns nothing — the view model lives in RootView so
/// the conversation survives the Face ID lock swap, and conversations
/// themselves live on the server (see AskCavnarViewModel).
struct AskCavnarView: View {
    @Bindable var viewModel: AskCavnarViewModel
    /// True while another tab is selected. TabView keeps this view mounted
    /// and its TimelineViews ticking behind the other tabs; the orbs
    /// freeze so they cost nothing there.
    var motionPaused: Bool = false

    @FocusState private var inputFocused: Bool
    @State private var showingHistory = false
    @State private var scrollThrottle = ScrollThrottle()

    private let suggestedQuestions = [
        "How are my reviews doing?",
        "What upcoming holidays should I focus on?",
        "How do I get my labor cost down?",
    ]

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                header

                ScrollViewReader { proxy in
                    ScrollView {
                        // LazyVStack, not VStack — a plain VStack forces
                        // SwiftUI to build and lay out every bubble in the
                        // whole conversation (including full UIKit text
                        // measurement for user bubbles) on every re-render.
                        // LazyVStack only builds what's on screen.
                        LazyVStack(alignment: .leading, spacing: 16) {
                            if viewModel.isOpeningConversation {
                                CavnarLoadingSeal()
                                    .padding(.top, 60)
                                    .frame(maxWidth: .infinity)
                            } else if viewModel.messages.isEmpty {
                                emptyState
                            }
                            ForEach(viewModel.messages) { message in
                                ChatBubble(message: message, viewModel: viewModel, motionPaused: motionPaused) {
                                    // Fires on every word TypewriterText
                                    // reveals — the bubble grows taller over
                                    // that ~1.4s window entirely on the client
                                    // side, so nothing else would re-scroll as
                                    // the reveal grew it and a long answer's
                                    // tail ended up hidden behind the
                                    // keyboard. No withAnimation — an animated
                                    // scroll re-triggered on every word would
                                    // stack/fight itself at this frequency.
                                    // Targets the trailing spacer below (not
                                    // the bubble's own id) so a consistent gap
                                    // stays above the input bar. Throttled to
                                    // ~10/sec (audit 3.3).
                                    let now = Date()
                                    guard now.timeIntervalSince(scrollThrottle.last) > 0.1 else { return }
                                    scrollThrottle.last = now
                                    proxy.scrollTo(chatScrollBottomID, anchor: .bottom)
                                }
                                .id(message.id)
                            }
                            if viewModel.isLoading {
                                LoadingBubble(label: viewModel.statusLabel, orbState: viewModel.orbState,
                                              paused: motionPaused)
                            }
                            // Scroll target for the in-progress reveal above
                            // — reserved space the scroll view can settle into.
                            Color.clear
                                .frame(height: 8)
                                .id(chatScrollBottomID)
                        }
                        .padding(16)
                    }
                    // .immediately, not .interactively — interactive mode
                    // installs its own pan gesture recognizer on the scroll
                    // view, which competed with taps on the text field below
                    // (every tap had to wait for the maybe-a-drag to fail
                    // first). .immediately still lets a scroll dismiss the
                    // keyboard, without the second recognizer.
                    .scrollDismissesKeyboard(.immediately)
                    // anchor: .center — a new bubble landing flush against
                    // the input bar read as abrupt/cramped; centering it
                    // gives it breathing room, the settle-into-view feel
                    // most chat apps use.
                    .onChange(of: viewModel.messages.count) { _, _ in
                        if let last = viewModel.messages.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .center) }
                        }
                    }
                    // A reopened chat lands scrolled to its latest turn, no
                    // animation — it's a page, not a new message arriving.
                    .onChange(of: viewModel.conversationId) { _, _ in
                        proxy.scrollTo(chatScrollBottomID, anchor: .bottom)
                    }
                    // Tapping into the field to compose opened the keyboard
                    // over the lower half of the transcript with nothing
                    // scrolling to compensate. Scroll to the reserved bottom
                    // spacer once focus lands; the short delay lets the
                    // keyboard's own presentation animation start first.
                    .onChange(of: inputFocused) { _, focused in
                        guard focused else { return }
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                            withAnimation(.easeOut(duration: 0.25)) {
                                proxy.scrollTo(chatScrollBottomID, anchor: .bottom)
                            }
                        }
                    }
                }

                inputBar
            }
            .cavnarModuleBackground()
            .navigationBarTitleDisplayMode(.inline)
            .toolbar(.hidden, for: .navigationBar)
            // No .keyboardDoneToolbar here (unlike this app's other screens)
            // — the header's own "Done" button dismisses the keyboard. Both
            // used to be applied together: the keyboard-toolbar checkmark
            // rendered right where the input bar's Send button sits.
            .navigationDestination(isPresented: $showingHistory) {
                AskCavnarHistoryView(viewModel: viewModel)
            }
            .task { await viewModel.loadInitialIfNeeded() }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            // Idle orb — a slow breathing ring while nothing is in flight.
            CavnarOrb(state: .breathing, size: 40, paused: motionPaused)
            VStack(alignment: .leading, spacing: 2) {
                Text("Ask Cavnar AI")
                    .font(.cavnarHeadline(20.5))
                    .foregroundStyle(Color.cavnarInk)
                Text("Your restaurant intelligence consultant")
                    .font(.cavnarBody(14.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }
            Spacer(minLength: 8)
            if inputFocused {
                // Fixed top-right, not a .keyboard-placement toolbar item —
                // that accessory row floats in the exact strip the input
                // bar's Send button occupies. Only while there's a keyboard
                // to dismiss.
                Button("Done") { inputFocused = false }
                    .font(.cavnarBody(15.5, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                    .transition(.opacity)
            } else {
                HStack(spacing: 8) {
                    headerChip("clock.arrow.circlepath", label: "Chat history") {
                        showingHistory = true
                    }
                    headerChip("square.and.pencil", label: "New chat") {
                        viewModel.startNewChat()
                    }
                    .disabled(viewModel.isLoading)
                    .opacity(viewModel.isLoading ? 0.45 : 1)
                }
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: inputFocused)
        .padding(.horizontal, 20)
        .padding(.top, 18)
        .padding(.bottom, 14)
    }

    /// The same glass chip as every toolbar icon in the app — the header
    /// here stands in for a navigation bar, so its controls match one.
    private func headerChip(_ systemImage: String, label: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Image(systemName: systemImage)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
                .cavnarToolbarIconGlass(size: 34)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private var emptyState: some View {
        VStack(spacing: 18) {
            GlowBadge(systemImage: "sparkles", size: 64)
                .padding(.top, 20)

            VStack(spacing: 6) {
                Text("Ask me anything")
                    .font(.cavnarHeadline(21.5))
                    .foregroundStyle(Color.cavnarInk)
                Text("Your numbers, or general advice on running the place — I'll pull in your real data whenever it's relevant.")
                    .font(.cavnarBody(14.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .multilineTextAlignment(.center)
                    .lineSpacing(3)
                    .padding(.horizontal, 24)
            }

            VStack(spacing: 8) {
                ForEach(suggestedQuestions, id: \.self) { question in
                    Button {
                        Haptic.light()
                        viewModel.question = question
                        Task { await viewModel.submit() }
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "sparkle")
                                .font(.system(size: 12, weight: .semibold))
                            Text(question)
                                .font(.cavnarBody(14.5, weight: 600))
                            Spacer()
                        }
                        .foregroundStyle(Color.cavnarEmber2)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 11)
                        .background(Color.cavnarEmber.opacity(0.10))
                        .overlay(
                            RoundedRectangle(cornerRadius: CavnarRadius.control)
                                .strokeBorder(Color.cavnarEmber.opacity(0.25), lineWidth: 1)
                        )
                        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 4)

            // Past chats are one tap away even from an empty screen — the
            // header chip is small, and a returning owner looks here first.
            if !viewModel.conversations.isEmpty {
                Button {
                    Haptic.light()
                    showingHistory = true
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "clock.arrow.circlepath")
                            .font(.system(size: 12, weight: .semibold))
                        Text("\(viewModel.conversations.count) earlier \(viewModel.conversations.count == 1 ? "chat" : "chats")")
                            .font(.cavnarBody(14, weight: 600))
                    }
                    .foregroundStyle(Color.cavnarInk3)
                }
                .buttonStyle(.plain)
                .padding(.top, 6)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.bottom, 20)
    }

    // No outer bar at all — the field and send button float directly on
    // the module background, each carrying its own glass, the same way
    // this app's other floating controls (the keyboard's Done pill, the
    // toolbar icon chips) already do rather than sitting inside a flat
    // full-width band. A flat .ultraThinMaterial strip behind the whole
    // row used to double up on top of the field's own pill background —
    // one piece of chrome the field didn't need.
    private var inputBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let error = viewModel.errorBanner {
                Text(error)
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarRed)
                    .padding(.horizontal, 4)
            }
            // Only surfaced as the cap approaches — the server truncates at
            // 2000 characters silently, so the limit has to be visible before
            // it bites (audit 5.2).
            if viewModel.remainingCharacters < 80 {
                Text("\(viewModel.remainingCharacters)")
                    .font(.cavnarNumber(12, weight: 600))
                    .foregroundStyle(viewModel.remainingCharacters <= 0 ? Color.cavnarRed : Color.cavnarInk3)
                    .padding(.horizontal, 4)
            }
            inputRow
        }
        .padding(.horizontal, 14)
        .padding(.top, 8)
        .padding(.bottom, 12)
    }

    private var inputRow: some View {
        HStack(alignment: .bottom, spacing: 10) {
            Group {
                if #available(iOS 26.0, *) {
                    // Real Liquid Glass — same tint-strong-enough-to-
                    // dominate-the-background approach as the keyboard's
                    // Done pill (cavnarToolbarPillGlass), so the field
                    // reads as a predictable warm surface rather than
                    // whatever's scrolling behind it refracting through.
                    fieldContent
                        .glassEffect(
                            .regular.tint(Color.cavnarPaper2.opacity(0.92)).interactive(),
                            in: RoundedRectangle(cornerRadius: CavnarRadius.sheet)
                        )
                } else {
                    fieldContent
                        .background(Color.cavnarPaper2)
                        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.sheet))
                }
            }
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.sheet)
                    .strokeBorder(inputFocused ? Color.cavnarEmber.opacity(0.5) : Color.white.opacity(0.06), lineWidth: 1)
            )
            .animation(.easeOut(duration: 0.15), value: inputFocused)

            Button {
                Haptic.light()
                Task { await viewModel.submit() }
            } label: {
                sendGlyph
                    .shadow(color: viewModel.canSubmit ? Color.cavnarEmber.opacity(0.4) : .clear, radius: 8, y: 3)
                    // Visual stays 38pt; the hit region meets the 44pt HIG
                    // minimum — this is the primary action of the AI
                    // screen (audit 7.3).
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .disabled(!viewModel.canSubmit)
            .animation(.easeOut(duration: 0.15), value: viewModel.canSubmit)
            .accessibilityLabel("Send question")
            .accessibilityHint(viewModel.canSubmit ? "" : "Type a question first")
        }
    }

    private var fieldContent: some View {
        TextField("How can I help?", text: $viewModel.question, axis: .vertical)
            .font(.cavnarBody(15.5))
            .foregroundStyle(Color.cavnarInk)
            .focused($inputFocused)
            .lineLimit(1...5)
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
    }

    /// The send button itself: real glass on iOS 26 (tinted ember when
    /// there's something to send, a neutral glass when there isn't — same
    /// prominent/plain distinction CavnarGlassButtonStyle uses for
    /// Skip/Approve), the previous solid gradient/flat-fill circle as the
    /// fallback below it.
    @ViewBuilder
    private var sendGlyph: some View {
        let icon = Image(systemName: "arrow.up")
            .font(.system(size: 16, weight: .bold))
            .foregroundStyle(viewModel.canSubmit ? .white : Color.cavnarInk3)
            .frame(width: 38, height: 38)
        if #available(iOS 26.0, *) {
            icon.glassEffect(
                viewModel.canSubmit
                    ? .regular.tint(Color.cavnarEmber.opacity(0.85)).interactive()
                    : .regular.interactive(),
                in: Circle()
            )
        } else {
            icon.background(Circle().fill(sendButtonFill))
        }
    }

    private var sendButtonFill: AnyShapeStyle {
        viewModel.canSubmit
            ? AnyShapeStyle(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber], startPoint: .top, endPoint: .bottom))
            : AnyShapeStyle(Color.cavnarPaper3)
    }
}

/// Same asymmetric "tail" corner on every bubble — the corner nearest the
/// avatar/sender stays sharp (4pt), the other three stay fully rounded
/// (18pt) — the standard chat-bubble directional cue (iMessage etc.).
private func chatBubbleShape(isUser: Bool) -> UnevenRoundedRectangle {
    UnevenRoundedRectangle(
        topLeadingRadius: 18,
        bottomLeadingRadius: isUser ? 18 : 4,
        bottomTrailingRadius: isUser ? 4 : 18,
        topTrailingRadius: 18
    )
}

private struct ChatBubble: View {
    let message: ChatMessage
    var viewModel: AskCavnarViewModel? = nil
    var motionPaused: Bool = false
    var onReveal: (() -> Void)? = nil

    // Bubble's outer cap, minus its own horizontal padding (15pt each
    // side) — the width actually available to the text itself.
    private static let maxBubbleWidth: CGFloat = 280
    private static let maxTextWidth: CGFloat = maxBubbleWidth - 30
    /// Measurement font for cavnarMeasuredTextWidth. Falls back to the system
    /// font rather than force-unwrapping: UIFont(name:size:) returns nil if the
    /// custom font fails to register, and this is a static let on the app's
    /// main AI screen — the unwrap crashed the whole surface (audit 2.1).
    private static let baseTextFont: UIFont =
        UIFont(name: "ApfelGrotezk-Regular", size: 16) ?? .systemFont(ofSize: 16)

    /// Scaled to the user's current text size. Measuring with a frozen 16pt
    /// font while the rendered Text scales with Dynamic Type would under-
    /// measure and clip every bubble (audit 7.1/7.2).
    private static var textFont: UIFont {
        UIFontMetrics(forTextStyle: .body).scaledFont(for: baseTextFont)
    }

    // Sidesteps SwiftUI's content-hugging negotiation entirely (four
    // attempts at fixedSize/frame(maxWidth:) ordering all failed — "Yes"
    // kept rendering at the full maxWidth): cavnarMeasuredTextWidth
    // measures the real rendered width via UIKit, and that exact number is
    // applied via frame(width:), not frame(maxWidth:). See its comment.
    private var userTextWidth: CGFloat {
        cavnarMeasuredTextWidth(message.text, font: Self.textFont, maxWidth: Self.maxTextWidth)
    }

    var body: some View {
        if let parts = message.statusParts {
            statusLine(parts)
        } else {
            bubble
        }
    }

    /// "[Confirmed: Email Fresh Co]" — what the owner did with a proposal,
    /// as a small centered note. It's a record, not something they typed.
    private func statusLine(_ parts: (verb: String, label: String)) -> some View {
        let confirmed = parts.verb == "Confirmed"
        return HStack(spacing: 6) {
            Image(systemName: confirmed ? "checkmark.circle.fill" : "minus.circle")
                .font(.system(size: 11, weight: .bold))
            Text("\(parts.verb) · \(parts.label)")
                .font(.cavnarBody(12.5, weight: 600))
                .lineLimit(2)
                .multilineTextAlignment(.center)
        }
        .foregroundStyle(confirmed ? Color.cavnarGreen : Color.cavnarInk3)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background((confirmed ? Color.cavnarGreen : Color.cavnarInk3).opacity(0.1), in: Capsule())
        .frame(maxWidth: .infinity)
        .accessibilityLabel("\(parts.verb): \(parts.label)")
    }

    private var bubble: some View {
        HStack(alignment: .top, spacing: 8) {
            if !message.isUser {
                GlowBadge(systemImage: "sparkles", size: 28)
                    .padding(.top, 2)
            }

            VStack(alignment: .leading, spacing: 6) {
                if !message.isUser {
                    Text("CAVNAR AI")
                        .font(.cavnarBody(14, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                }
                if message.isUser {
                    Text(message.text)
                        .font(.cavnarBody(16))
                        .lineSpacing(5)
                        .foregroundStyle(.white)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(width: userTextWidth, alignment: .leading)
                } else {
                    // Word-by-word reveal, block by block (paragraphs,
                    // bullets, numbered lists, headings — see
                    // CavnarMarkdown) instead of the answer just snapping
                    // in. Plays once per message ever: hasRevealed lives on
                    // the model (see ChatMessage).
                    TypewriterText(
                        fullText: message.text, size: 16, color: Color.cavnarInk, lineSpacing: 5,
                        maxWidth: Self.maxTextWidth, measuringFont: Self.textFont,
                        onReveal: onReveal,
                        startRevealed: message.hasRevealed,
                        onComplete: { viewModel?.markRevealed(message.id) }
                    )
                    if message.wasTruncated {
                        // The model hit max_tokens, so this answer stops
                        // mid-thought. Saying so is the difference between
                        // advice and half a sentence read as advice (audit 5.1).
                        Label("Answer was cut short", systemImage: "text.append")
                            .font(.cavnarBody(13, weight: 600))
                            .foregroundStyle(Color.cavnarAmber)
                            .padding(.top, 4)
                    }
                    ForEach(message.proposals) { proposal in
                        ProposalCard(proposal: proposal, viewModel: viewModel)
                            .padding(.top, 10)
                    }
                }
            }
            .padding(.horizontal, 15)
            .padding(.vertical, 12)
            // The shadow sits on the background SHAPE, not on the finished
            // bubble. `.shadow` on a composite view renders that whole view
            // (text, proposal cards and all) to an offscreen buffer and
            // blurs it — and re-does it every time the content changes,
            // which during a typewriter reveal is every word. A shape's
            // shadow is a cheap, cached path shadow.
            .background(
                chatBubbleShape(isUser: message.isUser)
                    .fill(bubbleBackground)
                    .shadow(color: message.isUser ? Color.cavnarEmber.opacity(0.22) : Color.black.opacity(0.25),
                            radius: 10, x: 0, y: 4)
            )
            .overlay(
                chatBubbleShape(isUser: message.isUser)
                    .strokeBorder(message.isUser ? Color.white.opacity(0.15) : Color.white.opacity(0.06), lineWidth: 1)
            )
        }
        .frame(maxWidth: .infinity, alignment: message.isUser ? .trailing : .leading)
    }

    private var bubbleBackground: AnyShapeStyle {
        message.isUser
            ? AnyShapeStyle(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber], startPoint: .topLeading, endPoint: .bottomTrailing))
            : AnyShapeStyle(Color.cavnarPaper2)
    }
}

/// The in-flight bubble: the thinking orb, whose motion changes with what
/// the agent is doing right now, next to the streamed status label.
private struct LoadingBubble: View {
    /// What the assistant is doing right now, streamed from the backend's
    /// tool loop ("Reading your reviews"). nil while the stream is still
    /// connecting, or once it falls back to the plain non-streaming request.
    var label: String? = nil
    /// Drives the orb's motion — connecting, searching, composing and so on.
    var orbState: CavnarOrbState = .connecting
    var paused: Bool = false

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            CavnarOrb(state: orbState, size: 28, paused: paused)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 10) {
                Text("CAVNAR AI")
                    .font(.cavnarBody(13.5, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                if let label {
                    Text(label)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .transition(.opacity)
                } else {
                    // "Composing" — an ember caret writing lines into place
                    // while Cavnar thinks (see CavnarMotion).
                    CavnarComposingLines(widths: [0.8, 0.45, 0.65], lineHeight: 8, spacing: 8)
                        .frame(width: 150)
                }
            }
            .padding(.horizontal, 15)
            .padding(.vertical, 12)
            .background(Color.cavnarPaper2, in: chatBubbleShape(isUser: false))
            .overlay(
                chatBubbleShape(isUser: false)
                    .strokeBorder(Color.white.opacity(0.06), lineWidth: 1)
            )
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}


/// A proposed action, awaiting the owner's tap.
///
/// The assistant can compose and explain an action but never perform one
/// that leaves the building — this card is the confirmation step, and
/// confirming calls the same endpoint the app's own button uses.
private struct ProposalCard: View {
    let proposal: AskProposal
    var viewModel: AskCavnarViewModel?

    // `Phase`, not `State`: a nested type named State shadows SwiftUI's
    // @State wrapper and the file stops compiling.
    @State private var phase: Phase = .pending
    private enum Phase { case pending, working, done, failed }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(proposal.summary)
                .font(.cavnarBody(15, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)

            switch phase {
            case .done:
                Label("Done", systemImage: "checkmark.circle.fill")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarGreen)
            case .working:
                // CavnarShimmerText takes text + color only (see ViewModifiers);
                // it sets its own type. Same call shape as AddCompetitorSheet.
                CavnarShimmerText(text: "Working…", color: Color.cavnarInk)
            case .pending, .failed:
                HStack(spacing: 8) {
                    Button {
                        Task {
                            phase = .working
                            let ok = await viewModel?.confirm(proposal) ?? false
                            phase = ok ? .done : .failed
                        }
                    } label: {
                        Text("Confirm")
                            .font(.cavnarBody(14, weight: 700))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 16).padding(.vertical, 8)
                            .background(Color.cavnarEmber)
                            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    }
                    .buttonStyle(.plain)

                    Button {
                        Task { await viewModel?.dismiss(proposal) }
                        phase = .done
                    } label: {
                        Text("Not now")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.horizontal, 14).padding(.vertical, 8)
                            .overlay(
                                RoundedRectangle(cornerRadius: CavnarRadius.control)
                                    .strokeBorder(Color.cavnarPaper3, lineWidth: 1)
                            )
                    }
                    .buttonStyle(.plain)
                }
                if phase == .failed {
                    Text("That didn't go through — try again.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.cavnarPaper2.opacity(0.8))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.cavnarEmber.opacity(0.45), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}
