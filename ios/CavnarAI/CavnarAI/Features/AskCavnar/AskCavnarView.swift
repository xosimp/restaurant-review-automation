import SwiftUI
import UIKit

/// Scroll target used while Cavnar's answer is typing out — see the
/// comment at its scrollTo call site in AskCavnarView.body.
private let chatScrollBottomID = "chat-scroll-bottom"

/// The chat's scroll view, as a coordinate space: a bubble's minY in it is
/// where its top sits on screen (0 = the top of the visible chat).
private let chatSpace = "ask-chat"

/// Holds the reveal-driven scroll throttle's clock OUTSIDE SwiftUI's
/// dependency tracking. As an `@State var Date` this was written up to
/// 10x/sec during every answer's reveal, and each write invalidated the
/// whole tab's body — header, orb, input bar, every bubble — for a value
/// nothing on screen even reads. A reference type's property changing
/// invalidates nothing.
@MainActor
private final class ScrollThrottle {
    var last = Date.distantPast
    /// Where the newest answer's (or the in-flight bubble's) top sits in
    /// the chat's visible frame. The bottom-follow stops once it reaches
    /// the top (#36): from there the owner reads down from the start of
    /// the answer, never chased to its end.
    var liveTop: CGFloat = .infinity

    /// True while following the bottom still keeps the answer's start on
    /// screen.
    var shouldFollow: Bool { liveTop > 8 }

    /// Throttled to ~10/sec (audit 3.3).
    func tick() -> Bool {
        let now = Date()
        guard now.timeIntervalSince(last) > 0.1 else { return false }
        last = now
        return true
    }
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
    /// Voice Ask: the composer's mic (AskVoiceInput.swift).
    @State private var voice = AskVoiceInput()

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
                        LazyVStack(alignment: .leading, spacing: CavnarSpace.m) {
                            if viewModel.isOpeningConversation {
                                CavnarLoadingOrb()
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
                                    // side. It follows the growth only while
                                    // the answer's top is still below the top
                                    // of the chat (#36): once its first line
                                    // reaches the top, the owner reads down
                                    // from there instead of being carried to
                                    // the end. No withAnimation — an animated
                                    // scroll re-triggered on every word would
                                    // stack/fight itself at this frequency.
                                    // Targets the trailing spacer below (not
                                    // the bubble's own id) so a consistent gap
                                    // stays above the input bar.
                                    followBottom(proxy)
                                }
                                .id(message.id)
                                .reportsTop(message.id == viewModel.messages.last?.id && !message.isUser,
                                            into: scrollThrottle)
                            }
                            if viewModel.isLoading {
                                LoadingBubble(label: viewModel.statusLabel, trail: viewModel.progressTrail, orbState: viewModel.orbState,
                                              paused: motionPaused, preview: viewModel.streamingPreview)
                                    .reportsTop(true, into: scrollThrottle)
                            }
                            // Scroll target for the in-progress reveal above
                            // — reserved space the scroll view can settle into.
                            Color.clear
                                .frame(height: 8)
                                .id(chatScrollBottomID)
                        }
                        .padding(CavnarSpace.m)
                        // The conversation reads as one column on an iPad (#99).
                        .cavnarReadableWidth()
                    }
                    .coordinateSpace(.named(chatSpace))
                    // .immediately, not .interactively — interactive mode
                    // installs its own pan gesture recognizer on the scroll
                    // view, which competed with taps on the text field below
                    // (every tap had to wait for the maybe-a-drag to fail
                    // first). .immediately still lets a scroll dismiss the
                    // keyboard, without the second recognizer.
                    .scrollDismissesKeyboard(.immediately)
                    // A new answer lands with its top at the top of the chat
                    // (#36) — the headline first, read downward, never its
                    // tail. The owner's own question (and a confirm's status
                    // line) settles at the bottom, where the answer will grow.
                    .onChange(of: viewModel.messages.count) { _, _ in
                        guard let last = viewModel.messages.last else { return }
                        if last.isUser {
                            withAnimation { proxy.scrollTo(chatScrollBottomID, anchor: .bottom) }
                        } else {
                            // Until its geometry reports, nothing follows.
                            scrollThrottle.liveTop = 0
                            withAnimation { proxy.scrollTo(last.id, anchor: .top) }
                        }
                    }
                    // The streamed preview grows sentence by sentence; it is
                    // followed like a reveal, until its top reaches the top.
                    .onChange(of: viewModel.streamingPreview) { _, _ in
                        followBottom(proxy)
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
                    // .safeAreaInset, NOT a VStack sibling below the
                    // ScrollView. This is the one structural thing that
                    // made this tab different from Home/Modules/Account,
                    // and it lines up with the reported symptom: on iOS 26
                    // the tab bar's glass decides how translucent to be
                    // from the scroll view that reaches its edge. With the
                    // input bar as a sibling, this ScrollView STOPPED
                    // short of the bottom — no scroll view at the tab
                    // bar's edge to read — so the bar had to work that out
                    // some other way on every switch to this tab, which is
                    // the "whole row lags for a second then goes more
                    // transparent" behaviour, and only here. As an inset,
                    // the ScrollView owns the full height (content just
                    // gets padded out from under the bar), exactly like
                    // every other tab.
                    .safeAreaInset(edge: .bottom, spacing: 0) {
                        // The composer lines up with the column above it.
                        inputBar.cavnarReadableWidth().background(Color.cavnarPaper)
                    }
                }
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
            // The mic never stays live behind another tab or screen.
            .onChange(of: motionPaused) { _, paused in if paused { voice.stop() } }
            .onDisappear { voice.stop() }
            .task {
                await viewModel.loadInitialIfNeeded()
                // Runs on every appear; the view model's own TTL decides
                // whether that costs a request, so returning to the tab
                // after acting on something shows the briefing without it.
                await viewModel.loadOpening()
            }
        }
    }

    /// The bottom-follow for a growing answer or preview: throttled, and
    /// only while the answer's top is still below the top of the chat.
    private func followBottom(_ proxy: ScrollViewProxy) {
        guard scrollThrottle.shouldFollow, scrollThrottle.tick() else { return }
        proxy.scrollTo(chatScrollBottomID, anchor: .bottom)
    }

    private var header: some View {
        HStack(spacing: CavnarSpace.s) {
            // Idle orb — a slow breathing ring while nothing is in flight;
            // its reserved `listening` wave while the mic is live.
            CavnarOrb(state: voice.isListening ? .listening : .breathing, size: 40, paused: motionPaused)
            // The title only (iOS re-audit L3): the tagline under it told
            // the owner nothing they could act on.
            Text("Ask Cavnar AI")
                .cavnarText(.headline)
            Spacer(minLength: CavnarSpace.xs)
            if inputFocused {
                // Fixed top-right, not a .keyboard-placement toolbar item —
                // that accessory row floats in the exact strip the input
                // bar's Send button occupies. Only while there's a keyboard
                // to dismiss.
                Button {
                    inputFocused = false
                } label: {
                    Text("Done")
                        .cavnarText(.label, color: .cavnarEmber)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .transition(.opacity)
            } else {
                HStack(spacing: CavnarSpace.xs) {
                    headerChip("clock.arrow.circlepath", label: "Chat history") {
                        showingHistory = true
                    }
                    headerChip("square.and.pencil", label: "New chat") {
                        viewModel.startNewChat()
                    }
                    .disabled(viewModel.isLoading)
                    .opacity(viewModel.isLoading ? 0.6 : 1)
                }
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: inputFocused)
        .padding(.horizontal, CavnarSpace.gutter)
        .padding(.top, 18)
        .padding(.bottom, 14)
    }

    /// The same glass chip as every toolbar icon in the app — the header
    /// here stands in for a navigation bar, so its controls match one.
    /// The questions actually worth asking right now, falling back to the
    /// static three only when the opening could not be fetched.
    private var activeSuggestions: [String] {
        let live = viewModel.opening?.suggestions ?? []
        return live.isEmpty ? suggestedQuestions : live
    }

    private func briefing(_ opening: AskOpening, headline: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            CavnarMixedText(headline, role: .headline)
            if let subtitle = briefingSubtitle(opening) {
                Text(subtitle)
                    .cavnarText(.caption)
                    .padding(.top, CavnarSpace.xxs)
            }
            ForEach(Array((opening.briefing ?? []).enumerated()), id: \.element.id) { index, item in
                if index > 0 {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                }
                // A tap asks about it (iOS re-audit L2) — the briefing
                // named what needs the owner, then gave no way in.
                Button {
                    Haptic.light()
                    viewModel.question = Self.briefingQuestion(item)
                    Task { await viewModel.submit() }
                } label: {
                    HStack(alignment: .top, spacing: 10) {
                        Circle()
                            .fill(severityTone(item.severity))
                            .frame(width: 7, height: 7)
                            .padding(.top, 7)
                        VStack(alignment: .leading, spacing: 2) {
                            CavnarMixedText(item.title ?? "", role: .label)
                            if let detail = item.detail, !detail.isEmpty {
                                CavnarMixedText(detail, role: .secondary)
                            }
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "arrow.up.right")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarEmber2)
                            .padding(.top, 4)
                            .accessibilityHidden(true)
                    }
                    .padding(.vertical, 10)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(viewModel.isLoading)
                .accessibilityHint("Asks Cavnar AI about this")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, CavnarSpace.xs)
    }

    /// What a briefing row asks when tapped: its own title, as a question.
    static func briefingQuestion(_ item: AskOpening.Item) -> String {
        let title = (item.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return title.isEmpty ? "What needs my attention today?" : "Tell me more about this: \(title)"
    }

    private func briefingSubtitle(_ opening: AskOpening) -> String? {
        let parts = [opening.restaurant, opening.sinceLabel].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func severityTone(_ severity: String?) -> Color {
        switch severity {
        case "critical": return .cavnarRed
        case "important": return .cavnarAmber
        case "good": return .cavnarGreen
        default: return .cavnarInk3
        }
    }

    private func headerChip(_ systemImage: String, label: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Image(systemName: systemImage)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
                .cavnarToolbarIconGlass(size: 34)
                .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    /// What the screen says before the owner types anything.
    ///
    /// It used to be a badge, "Ask me anything" and three hardcoded
    /// questions that never changed. The briefing and the questions now come
    /// from the same signals the Home tab reads, so the first thing an owner
    /// sees is their own business rather than an invitation to explain it.
    private var emptyState: some View {
        VStack(spacing: 18) {
            // Before anything is asked the screen shows Cavnar AI itself: the
            // Ember Core, large and breathing (10/8/26; the web's Ask panel
            // does the same). The dotted orb stays for working and loading.
            // ~56pt (L2): at 96pt with 42pt of padding the core pushed the
            // briefing below the fold.
            EmberCoreView(size: 56)
                .padding(.top, CavnarSpace.m)
                .padding(.bottom, CavnarSpace.xxs)
            if let opening = viewModel.opening, let headline = opening.headline {
                briefing(opening, headline: headline)
            } else {
                VStack(spacing: 6) {
                    Text("Ask me anything")
                        .cavnarText(.headline)
                    Text("Your numbers, or general advice on running the place — I'll pull in your real data whenever it's relevant.")
                        .cavnarText(.body)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
            }

            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Start here")
                    .frame(maxWidth: .infinity, alignment: .leading)
                ForEach(activeSuggestions, id: \.self) { question in
                    AskQuestionChip(question: question) {
                        viewModel.question = question
                        Task { await viewModel.submit() }
                    }
                }
            }
            .padding(.top, CavnarSpace.xxs)

            // Past chats are one tap away even from an empty screen — the
            // header chip is small, and a returning owner looks here first.
            if !viewModel.conversations.isEmpty {
                Button {
                    Haptic.light()
                    showingHistory = true
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "clock.arrow.circlepath")
                            .font(.cavnar(.secondary))
                        HomeMixedText.make("\(viewModel.conversations.count) earlier \(viewModel.conversations.count == 1 ? "chat" : "chats")",
                                           role: .label, color: .cavnarInk2)
                    }
                    .foregroundStyle(Color.cavnarInk2)
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.bottom, CavnarSpace.gutter)
    }

    // The gray band is gone (that was .ultraThinMaterial doubling up on
    // top of the field's own pill) — what's here instead is the PAGE
    // colour, which is invisible against the background behind it, so the
    // field and send button still read as floating with no container.
    //
    // Deliberately opaque rather than nothing, and deliberately not glass.
    // This is the only tab that puts anything in the bottom safe area, so
    // it was the only one whose bottom edge sat a translucent surface
    // (first the material band, then .glassEffect controls) immediately
    // above the tab bar's own Liquid Glass. Two glass surfaces meeting
    // there is exactly the kind of thing the tab bar has to reconcile on
    // appearance, which fits the reported "the whole row lags for a second
    // then goes even more transparent — only on this tab". An opaque
    // backdrop gives the bar the same ordinary surface to composite
    // against that Home, Modules and Account all give it. It also stops
    // chat text scrolling visibly behind the floating pill.
    private var inputBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let error = viewModel.errorBanner {
                Text(error)
                    .cavnarText(.secondary, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, CavnarSpace.xxs)
            }
            AskVoiceStatus(voice: voice)
                .animation(.easeOut(duration: 0.2), value: voice.isListening)
            // Only surfaced as the cap approaches — the server truncates at
            // 2000 characters silently, so the limit has to be visible before
            // it bites (audit 5.2).
            if viewModel.remainingCharacters < 80 {
                HomeMixedText.make("\(viewModel.remainingCharacters)", role: .caption,
                                   color: viewModel.remainingCharacters <= 0 ? Color.cavnarRedText : Color.cavnarInk3)
                    .padding(.horizontal, CavnarSpace.xxs)
            }
            inputRow
        }
        .padding(.horizontal, 14)
        .padding(.top, CavnarSpace.xs)
        .padding(.bottom, CavnarSpace.s)
        .background(Color.cavnarPaper)
    }

    private var inputRow: some View {
        HStack(alignment: .bottom, spacing: 10) {
            // A solid pill, not .glassEffect — see inputBar's comment. Glass
            // this close to the tab bar's own glass is the thing being
            // ruled out; the pill looked the same either way.
            fieldContent
                .background(Color.cavnarPaper2)
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.sheet))
                .overlay(
                    RoundedRectangle(cornerRadius: CavnarRadius.sheet)
                        .strokeBorder(inputFocused ? Color.cavnarEmber.opacity(0.5) : Color.white.opacity(0.06), lineWidth: 1)
                )
                .animation(.easeOut(duration: 0.15), value: inputFocused)

            // Voice types into the field; the owner still sends it.
            AskMicButton(voice: voice, disabled: viewModel.isLoading || viewModel.isOpeningConversation) {
                inputFocused = false
                Task {
                    await voice.toggle(read: { viewModel.question },
                                       write: { viewModel.question = $0 })
                }
            }

            Button {
                Haptic.light()
                // Sending ends a take in progress; what it heard is already
                // in the field being sent.
                voice.stop()
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
            .font(.cavnar(.body))
            .foregroundStyle(Color.cavnarInk)
            .focused($inputFocused)
            .lineLimit(1...5)
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
    }

    /// A solid ember circle, not glass — same reasoning as the field (see
    /// inputBar). It also keeps the app's own convention that the primary
    /// action on a screen is solid ember, not a translucent surface.
    private var sendGlyph: some View {
        Image(systemName: "arrow.up")
            .font(.system(size: 16, weight: .bold))
            .foregroundStyle(viewModel.canSubmit ? .white : Color.cavnarInk3)
            .frame(width: 38, height: 38)
            .background(Circle().fill(sendButtonFill))
    }

    private var sendButtonFill: AnyShapeStyle {
        viewModel.canSubmit
            ? AnyShapeStyle(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber], startPoint: .top, endPoint: .bottom))
            : AnyShapeStyle(Color.cavnarPaper3)
    }
}

private extension View {
    /// Reports this bubble's top (in the chat's visible frame) to the
    /// scroll follow, when it is the newest answer or the in-flight bubble.
    func reportsTop(_ enabled: Bool, into throttle: ScrollThrottle) -> some View {
        onGeometryChange(for: CGFloat.self) { proxy in
            proxy.frame(in: .named(chatSpace)).minY
        } action: { top in
            if enabled { throttle.liveTop = top }
        }
    }
}

/// One question to ask, as a tappable ember chip — the empty state's
/// "Start here" questions and the follow-ups under an answer (#91).
private struct AskQuestionChip: View {
    let question: String
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Image(systemName: "sparkle")
                    .font(.cavnar(.caption))
                    .accessibilityHidden(true)
                HomeMixedText.make(question, role: .label, color: .cavnarEmber2)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .foregroundStyle(Color.cavnarEmber2)
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .frame(minHeight: 44)
            .background(Color.cavnarEmber.opacity(0.10))
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(Color.cavnarEmber.opacity(0.25), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint("Asks Cavnar AI this question")
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

    // The owner's bubble's outer cap, minus its own horizontal padding
    // (15pt each side) — the width actually available to the text itself.
    private static let maxBubbleWidth: CGFloat = 280
    private static let maxTextWidth: CGFloat = maxBubbleWidth - 30
    /// Measurement font for cavnarMeasuredTextWidth. Falls back to the system
    /// font rather than force-unwrapping: UIFont(name:size:) returns nil if the
    /// custom font fails to register, and this is a static let on the app's
    /// main AI screen — the unwrap crashed the whole surface (audit 2.1).
    private static let baseTextFont: UIFont =
        UIFont(name: "ApfelGrotezk-Regular", size: CavnarText.body.size) ?? .systemFont(ofSize: CavnarText.body.size)

    /// Scaled to the user's current text size, by the Body role's own text
    /// style — measuring with a frozen font (or another style) while the
    /// rendered Text scales would under-measure and clip every bubble
    /// (audit 7.1/7.2).
    private static var textFont: UIFont {
        UIFontMetrics(forTextStyle: CavnarText.body.uiTextStyle).scaledFont(for: baseTextFont)
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
                .font(.cavnar(.caption))
            Text("\(parts.verb) · \(parts.label)")
                .cavnarText(.caption, color: confirmed ? Color.cavnarGreen : Color.cavnarInk2)
                .lineLimit(2)
                .multilineTextAlignment(.center)
        }
        .foregroundStyle(confirmed ? Color.cavnarGreen : Color.cavnarInk2)
        .padding(.horizontal, CavnarSpace.s)
        .padding(.vertical, 6)
        .background((confirmed ? Color.cavnarGreen : Color.cavnarInk3).opacity(0.1), in: Capsule())
        .frame(maxWidth: .infinity)
        .accessibilityLabel("\(parts.verb): \(parts.label)")
    }

    private var bubble: some View {
        HStack(alignment: .top, spacing: CavnarSpace.xs) {
            // The avatar names the speaker; the bubble carries no "CAVNAR AI"
            // label of its own any more (iOS readability round).
            if !message.isUser {
                GlowBadge(systemImage: "sparkles", size: 28)
                    .padding(.top, 2)
            }

            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                if message.isUser {
                    Text(message.text)
                        .cavnarText(.body, color: .white)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(width: userTextWidth, alignment: .leading)
                } else {
                    AskAnswerBody(message: message, viewModel: viewModel, onReveal: onReveal)
                }
            }
            // An answer takes the whole column: the card, the evidence and
            // the actions all need the width a hugging bubble never gave.
            .frame(maxWidth: message.isUser ? nil : .infinity, alignment: .leading)
            .padding(.horizontal, 15)
            .padding(.vertical, CavnarSpace.s)
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

// MARK: - The answer

/// Splitting an answer's text for the phone: the first paragraph to lead
/// with, and the lines the action list already shows taken out (#33, #34).
/// Pure, so the rules are pinned by tests.
enum AskAnswerText {
    /// The answer with the iPhone contract's scaffolding taken out (iOS
    /// re-audit H1): a bare "---" rule line is never drawn as text, and a
    /// "Follow-ups: a | b | c" line becomes the questions themselves — the
    /// "Ask next" chips — never a line of prose. The server strips both for
    /// a new answer; a reopened older turn, the streamed preview and an
    /// older server still carry them.
    static func scaffoldingStripped(_ text: String) -> (text: String, followUps: [String]) {
        var kept: [String] = []
        var ups: [String] = []
        for line in text.components(separatedBy: "\n") {
            if line.range(of: #"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$"#, options: .regularExpression) != nil {
                kept.append("")
                continue
            }
            if let r = line.range(of: #"^\s*(?:[-*•]\s+)?\**\s*(?:follow[- ]?ups?|ask next)\s*(?::\s*\**|\**\s*:)\s*"#,
                                  options: [.regularExpression, .caseInsensitive]) {
                if ups.isEmpty {
                    let rest = String(line[r.upperBound...])
                    ups = rest.components(separatedBy: "|")
                        .map { $0.replacingOccurrences(of: "**", with: "")
                            .trimmingCharacters(in: CharacterSet(charactersIn: " -–—\t")) }
                        .filter { $0.count >= 8 && $0.count <= 140 }
                    ups = Array(ups.prefix(3))
                }
                continue
            }
            kept.append(line)
        }
        var out: [String] = []
        for line in kept {
            let blank = line.trimmingCharacters(in: .whitespaces).isEmpty
            if blank, let last = out.last, last.trimmingCharacters(in: .whitespaces).isEmpty { continue }
            out.append(line)
        }
        return (out.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines), ups)
    }

    /// The text without any line that is one of `actions` (a suggestion the
    /// answer's own "Worth doing" list or the card shows): each action
    /// appears once, as the thing to answer, never again in the prose.
    /// Matched on the words, ignoring list markers, bold and case.
    static func removingLines(_ text: String, matching actions: [String]) -> String {
        let wanted = Set(actions.map(normalized).filter { !$0.isEmpty })
        guard !wanted.isEmpty else { return text }
        let kept = text.components(separatedBy: "\n").filter { !wanted.contains(normalized($0)) }
        // A blank run left where a list was is one paragraph break.
        var out: [String] = []
        for line in kept {
            let blank = line.trimmingCharacters(in: .whitespaces).isEmpty
            if blank, let last = out.last, last.trimmingCharacters(in: .whitespaces).isEmpty { continue }
            out.append(line)
        }
        return out.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// A line's words: no list marker, "Do first:" label, bold, punctuation
    /// at the ends or case.
    static func normalized(_ line: String) -> String {
        var t = line.trimmingCharacters(in: .whitespaces)
        if let r = t.range(of: #"^(?:[-*•]|\d{1,2}[.)])\s+"#, options: .regularExpression) { t.removeSubrange(r) }
        if let r = t.range(of: #"^\**\s*do\s+first\s*(?::\s*\**|\**\s*:)\s*"#,
                           options: [.regularExpression, .caseInsensitive]) { t.removeSubrange(r) }
        t = t.replacingOccurrences(of: "**", with: "")
        t = t.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        return t.trimmingCharacters(in: CharacterSet(charactersIn: " .;:!")).lowercased()
    }

    /// (lead, rest): the answer's first paragraph block — with the block
    /// after it when the first is only a heading — and everything after.
    /// `rest` is empty when there is nothing more.
    static func split(_ text: String) -> (lead: String, rest: String) {
        let lines = text.components(separatedBy: "\n")
        var i = 0
        while i < lines.count, lines[i].trimmingCharacters(in: .whitespaces).isEmpty { i += 1 }
        func isStructural(_ line: String) -> Bool {
            line.range(of: #"^\s*(?:#{1,6}\s|[-•]\s|\d+\.\s)"#, options: .regularExpression) != nil
        }
        func blockEnd(from start: Int) -> Int {
            guard start < lines.count else { return start }
            if isStructural(lines[start]) { return start + 1 }
            var j = start
            while j < lines.count, !lines[j].trimmingCharacters(in: .whitespaces).isEmpty,
                  j == start || !isStructural(lines[j]) { j += 1 }
            return j
        }
        var end = blockEnd(from: i)
        if i < lines.count, lines[i].range(of: #"^\s*#{1,6}\s"#, options: .regularExpression) != nil {
            var k = end
            while k < lines.count, lines[k].trimmingCharacters(in: .whitespaces).isEmpty { k += 1 }
            end = blockEnd(from: k)
        }
        let lead = lines[i..<min(end, lines.count)].joined(separator: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let rest = end < lines.count
            ? lines[end...].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines) : ""
        return (lead, rest)
    }

    /// Whether an answer without a card folds behind "Full answer" (#33):
    /// more than three blocks, or written to the executive contract — and
    /// only when there is something after the first block to fold.
    static func folds(_ text: String, depth: String?) -> Bool {
        let blocks = CavnarMarkdown.parse(text).count
        guard blocks > 1, !split(text).rest.isEmpty else { return false }
        return blocks > 3 || depth == "executive"
    }
}

/// The assistant's side of a bubble: the card when the answer has one, the
/// text (first paragraph first) when it does not.
private struct AskAnswerBody: View {
    let message: ChatMessage
    var viewModel: AskCavnarViewModel?
    var onReveal: (() -> Void)?

    var body: some View {
        if let card = message.card {
            AskCardAnswer(card: card, message: message, viewModel: viewModel)
        } else {
            AskPlainAnswer(message: message, viewModel: viewModel, onReveal: onReveal)
        }
    }
}

/// The confidence line an answer carries — the shared one (K5), "72%
/// confidence" in the one colour map, with "Why?".
private func askConfidenceLine(_ message: ChatMessage) -> ConfidenceLine? {
    guard let ev = message.evidence, let c = ev.confidence, ev.confidenceLabel != nil else { return nil }
    return ConfidenceLine(confidence: c, recKey: nil, surface: "ask", module: "ask")
}

/// The one compressed caveat line an answer carries, when anything in it
/// did not check out.
private func askCaveatSummary(_ message: ChatMessage) -> String? {
    if let s = message.evidence?.caveatSummary { return s }
    let n = message.caveats.count
    guard n > 0 else { return nil }
    return n == 1 ? "1 thing to check" : "\(n) things to check"
}

/// "Answer was cut short" — the model hit max_tokens, so the answer stops
/// mid-thought. Saying so is the difference between advice and half a
/// sentence read as advice (audit 5.1).
private struct AskCutShortNote: View {
    var body: some View {
        Label("Answer was cut short", systemImage: "text.append")
            .cavnarText(.secondary, color: .cavnarAmber)
    }
}

/// An answer written to the iPhone contract (#9): headline, the one
/// sentence, the cause, the first thing to do with its confirm card or its
/// Done / Pass row, what to expect and how sure — and everything else
/// behind "Full analysis". Proposals stay in full view: a confirm card
/// always shows exactly what will be sent before Confirm.
private struct AskCardAnswer: View {
    let card: AskCard
    let message: ChatMessage
    var viewModel: AskCavnarViewModel?

    /// The suggestion the Do first line was recorded as.
    private var actionSuggestion: AskSuggestion? {
        guard let key = card.actionKey else { return nil }
        return message.suggestions.first { $0.recKey == key }
    }

    /// The suggestion that leads the actions: the Do first line's own, or —
    /// when the card names no action and no confirm card leads — the first.
    private var leadSuggestion: AskSuggestion? {
        if let s = actionSuggestion { return s }
        if card.action == nil && message.proposals.isEmpty { return message.suggestions.first }
        return nil
    }

    private var otherSuggestions: [AskSuggestion] {
        let lead = leadSuggestion?.recKey
        return message.suggestions.filter { $0.recKey != lead }
    }

    /// The detail with every suggestion line taken out (#34): each action
    /// is answered once, where it is listed.
    private var detailText: String? {
        guard let d = message.detail else { return nil }
        let t = AskAnswerText.removingLines(AskAnswerText.scaffoldingStripped(d).text, matching: message.suggestions.map(\.text) + [card.action ?? ""])
        return t.isEmpty ? nil : t
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarAnswerCard(
                headline: card.headline,
                summary: card.summary,
                cause: card.cause,
                isHypothesis: card.causeFlagged,
                expectedOutcome: card.outcome,
                confidence: askConfidenceLine(message),
                detailLabel: "Full analysis",
                surface: nil
            ) {
                actions
            } detail: {
                detail
            }
            if message.wasTruncated {
                AskCutShortNote()
            }
            if !card.followUps.isEmpty {
                AskFollowUps(questions: card.followUps, viewModel: viewModel)
            }
        }
    }

    @ViewBuilder private var actions: some View {
        if let summary = askCaveatSummary(message) {
            AskCaveatLine(summary: summary, caveats: message.evidence?.caveatCards ?? [], extra: message.caveats)
        }
        if let action = card.action {
            (Text("Do first: ").font(.cavnar(.label)).foregroundStyle(Color.cavnarEmber2)
                + HomeMixedText.make(action, role: .label, color: .cavnarInk))
                .lineSpacing(CavnarText.label.lineSpacing)
                .fixedSize(horizontal: false, vertical: true)
        } else if let lead = leadSuggestion {
            CavnarMixedText(lead.text, role: .label, color: .cavnarInk)
        }
        if let first = message.proposals.first {
            ProposalCard(proposal: first, viewModel: viewModel)
        } else if let lead = leadSuggestion {
            RecAnswerRow(key: lead.recKey, surface: "ask", module: "ask")
        }
        ForEach(message.proposals.dropFirst()) { proposal in
            ProposalCard(proposal: proposal, viewModel: viewModel)
        }
    }

    @ViewBuilder private var detail: some View {
        if let text = detailText {
            TypewriterText(fullText: text, size: CavnarText.body.size, color: Color.cavnarInk, lineSpacing: 5,
                           startRevealed: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        if let evidence = message.evidence {
            AskEvidenceDetail(evidence: evidence)
        }
        if !otherSuggestions.isEmpty {
            AskSuggestionsBlock(suggestions: otherSuggestions)
        }
        if message.messageId != nil {
            AskFeedbackRow(message: message, viewModel: viewModel)
        }
    }
}

/// An answer without a card — an older server, a web-asked chat reopened,
/// or a reply that did not follow the contract. The text leads; a long or
/// executive one shows its first paragraph and folds the rest behind "Full
/// answer · N more" (#33); confidence and confirm cards sit right under it,
/// evidence below (#35); the action list owns the actions (#34).
private struct AskPlainAnswer: View {
    let message: ChatMessage
    var viewModel: AskCavnarViewModel?
    var onReveal: (() -> Void)?

    @State private var expanded = false
    @State private var showingSources = false

    /// The answer with the action list's own lines taken out (#34).
    private var bodyText: String {
        let t = AskAnswerText.removingLines(message.text, matching: message.suggestions.map(\.text))
        return t.isEmpty ? message.text : t
    }

    var body: some View {
        let text = bodyText
        let folds = AskAnswerText.folds(text, depth: message.depth)
        let parts = folds ? AskAnswerText.split(text) : (lead: text, rest: "")
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            // Word-by-word reveal, block by block (paragraphs, bullets,
            // numbered lists, headings — see CavnarMarkdown) instead of the
            // answer just snapping in. Plays once per message ever:
            // hasRevealed lives on the model (see ChatMessage).
            TypewriterText(
                fullText: parts.lead, size: CavnarText.body.size, color: Color.cavnarInk, lineSpacing: 5,
                onReveal: onReveal,
                startRevealed: message.hasRevealed,
                onComplete: { viewModel?.markRevealed(message.id) }
            )
            .frame(maxWidth: .infinity, alignment: .leading)
            if folds {
                if expanded {
                    TypewriterText(fullText: parts.rest, size: CavnarText.body.size, color: Color.cavnarInk,
                                   lineSpacing: 5, startRevealed: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .transition(.opacity)
                    if let evidence = message.evidence {
                        AskEvidenceDetail(evidence: evidence)
                    }
                    // Rated from inside the fold, as a card answer is
                    // (iOS re-audit L5).
                    if message.messageId != nil {
                        AskFeedbackRow(message: message, viewModel: viewModel)
                    }
                }
                AskDisclosureToggle(label: "Full answer \u{00B7} \(CavnarMarkdown.parse(parts.rest).count) more",
                                    isExpanded: $expanded)
            }
            if message.wasTruncated {
                AskCutShortNote()
            }
            if let summary = askCaveatSummary(message) {
                AskCaveatLine(summary: summary, caveats: message.evidence?.caveatCards ?? [], extra: message.caveats)
            }
            if let line = askConfidenceLine(message) {
                line
            }
            ForEach(message.proposals) { proposal in
                ProposalCard(proposal: proposal, viewModel: viewModel)
            }
            // The answer's own concrete advice, keyed — each one answerable
            // like any recommendation (#48).
            if !message.suggestions.isEmpty {
                AskSuggestionsBlock(suggestions: message.suggestions)
            }
            // The answer's own follow-ups as chips, never a "Follow-ups:" line (H1).
            if !message.followUps.isEmpty {
                AskFollowUps(questions: message.followUps, viewModel: viewModel)
            }
            // What it read and what the owner passed on before: proof, one
            // tap away (#35) — inside "Full answer" when the answer folds.
            // "Was this useful?" sits behind the same tap (L5) — it was on
            // the face of every plain answer.
            let hasSources = message.evidence?.hasDetail == true
            if !folds, hasSources || message.messageId != nil {
                if showingSources {
                    VStack(alignment: .leading, spacing: CavnarSpace.s) {
                        if hasSources, let evidence = message.evidence {
                            AskEvidenceDetail(evidence: evidence)
                        }
                        if message.messageId != nil {
                            AskFeedbackRow(message: message, viewModel: viewModel)
                        }
                    }
                    .transition(.opacity)
                }
                AskDisclosureToggle(label: hasSources ? "What this rests on" : "Rate this answer",
                                    isExpanded: $showingSources)
            }
        }
    }
}

/// A 44pt "Full answer · 3 more" / "Show less" toggle in the answer kit's
/// look (CavnarMoreToggle), with a label of its own.
private struct AskDisclosureToggle: View {
    let label: String
    @Binding var isExpanded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button {
            Haptic.light()
            if reduceMotion {
                isExpanded.toggle()
            } else {
                withAnimation(.easeOut(duration: 0.22)) { isExpanded.toggle() }
            }
        } label: {
            HStack(spacing: CavnarSpace.xxs + 2) {
                HomeMixedText.make(isExpanded ? "Show less" : label, role: .label, color: .cavnarEmber2)
                Image(systemName: "chevron.down")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarEmber2)
                    .rotationEffect(.degrees(isExpanded ? 180 : 0))
                    .accessibilityHidden(true)
                Spacer(minLength: 0)
            }
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
    }
}

/// "2 figures unverified · Details" — one amber line on the answer; its
/// Details opens the shared amber caveats in full ("Hypothesis" for a
/// cause). Replaces the red 12.5pt warning lines: an untraced figure is a
/// reason to check, not an error.
private struct AskCaveatLine: View {
    let summary: String
    let caveats: [CavnarCaveat]
    /// The validation's own caveats — shown when nothing more specific is.
    var extra: [String] = []
    @State private var open = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Button {
                Haptic.light()
                if reduceMotion { open.toggle() } else { withAnimation(.easeOut(duration: 0.2)) { open.toggle() } }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarAmber)
                        .accessibilityHidden(true)
                    (HomeMixedText.make(summary, role: .secondary, color: .cavnarAmber)
                        + Text(open ? " \u{00B7} Hide" : " \u{00B7} Details")
                            .font(.cavnar(.label)).foregroundStyle(Color.cavnarEmber2))
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityValue(open ? "Expanded" : "Collapsed")
            if open {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    if caveats.isEmpty {
                        if !extra.isEmpty {
                            CavnarCaveat(title: "Check before acting", detail: extra.joined(separator: " "))
                        }
                    } else {
                        ForEach(Array(caveats.enumerated()), id: \.offset) { _, caveat in
                            caveat
                        }
                    }
                }
                .transition(.opacity)
            }
        }
    }
}

/// The follow-up questions under a card (#91): two or three chips in the
/// empty state's style; a tap asks it.
private struct AskFollowUps: View {
    let questions: [String]
    var viewModel: AskCavnarViewModel?

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Ask next")
            ForEach(questions, id: \.self) { q in
                AskQuestionChip(question: q) {
                    Task { await viewModel?.askFollowUp(q) }
                }
                .disabled(viewModel?.isLoading == true)
                .opacity(viewModel?.isLoading == true ? 0.6 : 1)
            }
        }
        .padding(.top, CavnarSpace.xxs)
    }
}

/// What an answer rests on, for the detail (#35): the parts of the business
/// it actually read, as wrapping chips, and the advice in it the owner said
/// not for us to before. Proof, so it sits behind a tap.
private struct AskEvidenceDetail: View {
    let evidence: AskEvidence

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if !evidence.modules.isEmpty {
                AccountFlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(evidence.modules, id: \.self) { module in
                        Text(module)
                            .cavnarText(.tag, color: .cavnarInk2)
                            .padding(.horizontal, CavnarSpace.xs)
                            .padding(.vertical, 3)
                            .overlay(Capsule().stroke(Color.cavnarInk3.opacity(0.6), lineWidth: 1))
                    }
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Read: " + evidence.modules.joined(separator: ", "))
            }
            // Advice the owner already said not for us to, repeated in the
            // answer — kept and marked in the prose; said here once (M1).
            if let declined = evidence.declinedLine {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarInk2)
                        .accessibilityHidden(true)
                    CavnarMixedText(declined, role: .secondary)
                }
            }
        }
    }
}

extension AskEvidence {
    /// Anything for the "What this rests on" disclosure to hold.
    var hasDetail: Bool { !modules.isEmpty || declinedLine != nil }
}

/// The in-flight bubble: the thinking orb, whose motion changes with what
/// the agent is doing right now, next to the streamed status label.
private struct LoadingBubble: View {
    /// What the assistant is doing right now, streamed from the backend's
    /// tool loop ("Reading your reviews"). nil while the stream is still
    /// connecting, or once it falls back to the plain non-streaming request.
    var label: String? = nil
    /// What already ran this turn, oldest first — ticked lines above the
    /// one running now, so a multi-tool answer shows its reasoning rather
    /// than a single label that keeps changing.
    var trail: [String] = []
    /// Drives the orb's motion — connecting, searching, composing and so on.
    var orbState: CavnarOrbState = .connecting
    var paused: Bool = false
    /// The answer's validated sentences so far (AI cost audit 10/7/26 #68),
    /// rendered as the answer will be; the final answer replaces the whole
    /// bubble without retyping it. The chat follows it as it grows (#36).
    var preview: String = ""

    var body: some View {
        HStack(alignment: .top, spacing: CavnarSpace.xs) {
            CavnarOrb(state: orbState, size: 28, paused: paused)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 10) {
                if !preview.isEmpty {
                    TypewriterText(fullText: AskAnswerText.scaffoldingStripped(preview).text, size: CavnarText.body.size, color: Color.cavnarInk,
                                   lineSpacing: 5, startRevealed: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else if let label {
                    ForEach(Array(trail.enumerated()), id: \.offset) { _, done in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Image(systemName: "checkmark")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarGreen)
                            Text(done)
                                .cavnarText(.caption)
                        }
                        .transition(.opacity.combined(with: .move(edge: .leading)))
                    }
                    Text(label + "\u{2026}")
                        .cavnarText(.secondary)
                        .id(label)
                        .transition(.opacity)
                } else {
                    // "Composing" — an ember caret writing lines into place
                    // while Cavnar thinks (see CavnarMotion).
                    CavnarComposingLines(widths: [0.8, 0.45, 0.65], lineHeight: 8, spacing: 8)
                        .frame(width: 150)
                }
            }
            .padding(.horizontal, 15)
            .padding(.vertical, CavnarSpace.s)
            .background(Color.cavnarPaper2, in: chatBubbleShape(isUser: false))
            .overlay(
                chatBubbleShape(isUser: false)
                    .strokeBorder(Color.white.opacity(0.06), lineWidth: 1)
            )
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}


/// "Worth doing" — the answer's own list items that start with a verb and
/// carry no untraced figure, each with its measured confidence as a compact
/// meter and Done / Pass (surface `ask`).
private struct AskSuggestionsBlock: View {
    let suggestions: [AskSuggestion]

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Worth doing")
            ForEach(suggestions) { s in
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarMixedText(s.text, role: .label, color: .cavnarInk)
                    if let c = s.confidence {
                        let d = ConfidenceDisplay(c)
                        if d.isRenderable {
                            HStack(alignment: .center, spacing: CavnarSpace.xs) {
                                ConfidenceMeter(fraction: d.meterFraction, tone: d.tone)
                                HomeMixedText.make(d.lineLabel, role: .caption, color: .cavnarInk2)
                            }
                            .accessibilityElement(children: .ignore)
                            .accessibilityLabel(d.lineLabel)
                        }
                    }
                    RecAnswerRow(key: s.recKey, surface: "ask", module: "ask")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(CavnarSpace.s)
        .background(Color.cavnarEmber.opacity(0.06), in: RoundedRectangle(cornerRadius: CavnarRadius.control))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
            .strokeBorder(Color.cavnarEmber.opacity(0.22), lineWidth: 1))
    }
}

/// "Was this useful?" Yes / No under an answer — POST /ask-cavnar/feedback
/// with its message_id. A No then offers one optional line of what was
/// missing, sent as the same rating with `note` (as on web); Send with the
/// field empty just closes it. Once settled, a quiet line says so.
private struct AskFeedbackRow: View {
    let message: ChatMessage
    var viewModel: AskCavnarViewModel?
    @State private var busy = false
    @State private var note = ""
    @FocusState private var noteFocused: Bool

    var body: some View {
        if message.rating == false && !message.noteSettled {
            noteRow
        } else {
            ratingRow
        }
    }

    /// "What was missing?" — one optional line, then Send.
    private var noteRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("What was missing?")
                .cavnarText(.secondary)
            HStack(spacing: CavnarSpace.xs) {
                TextField("Optional", text: $note)
                    .font(.cavnar(.body))
                    .padding(.horizontal, 10)
                    .padding(.vertical, CavnarSpace.xs)
                    .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .foregroundStyle(Color.cavnarInk)
                    .focused($noteFocused)
                    .submitLabel(.send)
                    .onSubmit(send)
                    .onChange(of: note) { _, v in
                        if v.count > AskCavnarViewModel.feedbackNoteMax {
                            note = String(v.prefix(AskCavnarViewModel.feedbackNoteMax))
                        }
                    }
                    .accessibilityLabel("What was missing")
                Button("Send", action: send)
                    .buttonStyle(RecAnswerPillStyle())
                    .disabled(busy)
            }
        }
        .opacity(busy ? 0.6 : 1)
        .onAppear { noteFocused = true }
    }

    private func send() {
        guard !busy else { return }
        let text = note.trimmingCharacters(in: .whitespacesAndNewlines)
        noteFocused = false
        guard !text.isEmpty else {
            Haptic.light()
            viewModel?.skipFeedbackNote(message)
            return
        }
        Haptic.light()
        busy = true
        Task {
            _ = await viewModel?.rate(message, helpful: false, note: text)
            busy = false
        }
    }

    private var ratingRow: some View {
        HStack(spacing: CavnarSpace.xs) {
            if let rating = message.rating {
                Image(systemName: "checkmark").font(.cavnar(.caption))
                VStack(alignment: .leading, spacing: 3) {
                    Text(rating ? "Marked useful \u{2014} thanks" : "Noted \u{2014} this goes into future answers")
                        .cavnarText(.secondary)
                    // What the ratings now say about answer length — a
                    // preference kept for this login, forgettable in
                    // Account → Memory (M2).
                    if let pref = message.preferenceNote {
                        Text(pref)
                            .cavnarText(.secondary, color: .cavnarGreen)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            } else {
                Text("Was this useful?")
                    .cavnarText(.secondary)
                ForEach([true, false], id: \.self) { helpful in
                    Button {
                        Haptic.light()
                        busy = true
                        Task {
                            _ = await viewModel?.rate(message, helpful: helpful)
                            busy = false
                        }
                    } label: {
                        Text(helpful ? "Yes" : "No")
                    }
                    .buttonStyle(RecAnswerPillStyle())
                    .disabled(busy)
                }
            }
        }
        .foregroundStyle(Color.cavnarInk2)
        .opacity(busy ? 0.6 : 1)
        .animation(.easeOut(duration: 0.2), value: message.rating)
    }
}

/// A proposed action, awaiting the owner's tap.
///
/// The assistant can compose and explain an action but never perform one
/// that leaves the building — this card is the confirmation step, and
/// confirming calls the same endpoint the app's own button uses.
/// Also the command sheet's confirm card (Features/Command) — one card for
/// every proposal, wherever it was proposed.
struct ProposalCard: View {
    let proposal: AskProposal
    var viewModel: AskCavnarViewModel?
    /// Called once the confirmed action went through — Home's publish
    /// card re-reads Home and closes itself on it.
    var onDone: (() -> Void)?

    // Spelled out: the @State members below would make the synthesized
    // initializer private to this file.
    init(proposal: AskProposal, viewModel: AskCavnarViewModel? = nil, onDone: (() -> Void)? = nil) {
        self.proposal = proposal
        self.viewModel = viewModel
        self.onDone = onDone
    }

    // `Phase`, not `State`: a nested type named State shadows SwiftUI's
    // @State wrapper and the file stops compiling.
    @State private var phase: Phase = .pending
    private enum Phase { case pending, working, done, dismissed, failed, uncertain }
    /// Why the last Confirm didn't go through, in the server's words.
    @State private var failure: String?
    /// "Not now" asks why first — the six one-tap reasons every Not for us
    /// uses, or "Just not now" — before it dismisses.
    @State private var askingWhy = false
    /// What the confirmed route said beyond "Done" (a proposed goal).
    @State private var doneNote: String?
    /// The route's warning beside its ok (the web's `d.warning`).
    @State private var doneWarning: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarMixedText(proposal.summary, role: .label, color: .cavnarInk)

            // What confirming actually commits to: the money, the people it
            // reaches, the words that go out (#23).
            if let stake = proposal.atStake, stake > 0 {
                Text(stake, format: .currency(code: "USD"))
                    .cavnarText(.figureS, color: .cavnarInk)
            }
            if let details = proposal.details, !details.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    ForEach(details, id: \.self) { d in
                        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                            Text(d.label)
                                .cavnarText(.caption)
                            CavnarMixedText(d.value, role: .secondary)
                        }
                    }
                }
            }
            if let preview = proposal.preview, !preview.isEmpty {
                CavnarMixedText(preview, role: .secondary)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.cavnarPaper, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            }
            // Every field the confirmed route receives (NS5 C1) — what the
            // owner approves is what runs.
            if let shown = proposal.fieldsShown, !shown.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("Sent with this")
                    ForEach(shown, id: \.self) { f in
                        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                            Text(f.label)
                                .cavnarText(.caption)
                            CavnarMixedText(f.value, role: .secondary)
                        }
                    }
                }
            }

            switch phase {
            case .done:
                // A teammate's goal waits for the owner (M2): said, not
                // passed off as done.
                Label(doneNote ?? "Done", systemImage: "checkmark.circle.fill")
                    .cavnarText(.label, color: .cavnarGreen)
                // A decision the route flags (time off over a published
                // week): its warning stays on the card (parity audit #4).
                if let warning = doneWarning, !warning.isEmpty {
                    CavnarMixedText(warning, role: .secondary, color: .cavnarAmber)
                }
            case .dismissed:
                // Nothing was sent: said plainly in Ink2, no check (H2).
                Text("Not sent")
                    .cavnarText(.label, color: .cavnarInk2)
                    .accessibilityLabel("Not sent. You passed on this for now.")
            case .working:
                // CavnarShimmerText takes text + color only (see ViewModifiers);
                // it sets its own type. Same call shape as AddCompetitorSheet.
                CavnarShimmerText(text: "Working…", color: Color.cavnarInk)
            case .pending, .failed, .uncertain:
                HStack(spacing: CavnarSpace.xs) {
                    Button {
                        Task {
                            phase = .working
                            let ok = await viewModel?.confirm(proposal) ?? false
                            failure = ok ? nil : viewModel?.errorBanner
                            doneNote = ok ? viewModel?.lastConfirmNote : nil
                            doneWarning = ok ? viewModel?.lastConfirmWarning : nil
                            phase = ok ? .done : (viewModel?.lastConfirmMayHaveRun == true ? .uncertain : .failed)
                            if ok { onDone?() }
                        }
                    } label: {
                        // After a timeout the action may already have run,
                        // so a second tap is labelled for what it is.
                        Text(phase == .uncertain ? "Send again anyway" : "Confirm")
                            .cavnarText(.label, color: .white)
                            .padding(.horizontal, CavnarSpace.m)
                            .frame(minHeight: 44)
                            .background(Color.cavnarEmber)
                            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)

                    Button {
                        askingWhy = true
                    } label: {
                        Text("Not now")
                            .cavnarText(.label, color: .cavnarInk2)
                            .padding(.horizontal, 14)
                            .frame(minHeight: 44)
                            .overlay(
                                RoundedRectangle(cornerRadius: CavnarRadius.control)
                                    .strokeBorder(Color.cavnarPaper3, lineWidth: 1)
                            )
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                if phase == .failed || phase == .uncertain {
                    Text(failure ?? "That didn't go through — try again.")
                        .cavnarText(.secondary, color: phase == .uncertain ? Color.cavnarAmber : Color.cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
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
        .recReasonDialog(isPresented: $askingWhy, title: "Why not now?",
                         message: "Cavnar AI reads this before proposing it again.",
                         skipLabel: "Just not now",
                         onSkip: { dismissNow(nil) },
                         onPick: { reason in dismissNow(reason) })
    }

    /// "Not now" is a decision NOT to act — never the green "Done" a
    /// confirmed send shows (iOS re-audit H2).
    private func dismissNow(_ reason: RecReason?) {
        phase = .dismissed
        Task { await viewModel?.dismiss(proposal, reasonCode: reason) }
    }
}
