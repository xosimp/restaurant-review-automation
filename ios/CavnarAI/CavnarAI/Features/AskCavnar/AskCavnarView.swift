import SwiftUI
import UIKit

/// Scroll target used while Cavnar's answer is typing out — see the
/// comment at its scrollTo call site in AskCavnarView.body.
private let chatScrollBottomID = "chat-scroll-bottom"

struct AskCavnarView: View {
    /// Owned by RootView, not @State here — a sheet-owned view model is
    /// deallocated on every dismissal, so swiping down to check a figure wiped
    /// the whole conversation (audit 5.6). Same rationale as RootView's
    /// homeViewModel.
    /// @Bindable, not plain `let` — the input field binds to
    /// $viewModel.question, and the model is now owned by RootView.
    @Bindable var viewModel: AskCavnarViewModel
    @FocusState private var inputFocused: Bool
    /// Throttles the reveal-driven scroll — see its call site below.
    @State private var lastScrollTick = Date.distantPast

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
                        VStack(alignment: .leading, spacing: 16) {
                            if viewModel.messages.isEmpty {
                                emptyState
                            }
                            ForEach(viewModel.messages) { message in
                                ChatBubble(message: message) {
                                    // Fires on every word TypewriterText
                                    // reveals — the bubble grows taller
                                    // over that ~1.4s window entirely on
                                    // the client side (the message's full
                                    // text already arrived; onChange(of:
                                    // messages.count) below only fires
                                    // once, at that arrival). Without this,
                                    // nothing re-scrolled as the reveal
                                    // grew the bubble, so a long answer's
                                    // tail ended up hidden behind the
                                    // keyboard with no way to follow it.
                                    // No withAnimation — an animated scroll
                                    // re-triggered on every word would
                                    // stack/fight itself at this frequency;
                                    // a plain immediate scrollTo here reads
                                    // as smooth continuous tracking instead.
                                    // Targets the trailing spacer below
                                    // (chatScrollBottomID), not the bubble's
                                    // own id — scrolling straight to the
                                    // bubble put its bottom edge flush
                                    // against the input bar/keyboard with no
                                    // breathing room; anchoring on the
                                    // spacer that follows it reserves a
                                    // consistent gap instead, for as long as
                                    // this stays the last thing in the list.
                                    // Throttled to ~10/sec: a scroll
                                    // adjustment on every revealed word forced
                                    // a full scroll-view layout pass up to 60
                                    // times a second, on top of the reveal's
                                    // own work (audit 3.3). At this cadence it
                                    // still reads as continuous tracking.
                                    let now = Date()
                                    guard now.timeIntervalSince(lastScrollTick) > 0.1 else { return }
                                    lastScrollTick = now
                                    proxy.scrollTo(chatScrollBottomID, anchor: .bottom)
                                }
                                .id(message.id)
                            }
                            if viewModel.isLoading {
                                LoadingBubble()
                            }
                            // Scroll target for the in-progress reveal above
                            // — see its comment. Not part of the message
                            // list itself, just reserved space the scroll
                            // view can settle into.
                            Color.clear
                                .frame(height: 8)
                                .id(chatScrollBottomID)
                        }
                        .padding(16)
                    }
                    // Drag the message list and the keyboard tracks/dismisses
                    // with the gesture, same as Messages/Mail — previously
                    // there was no way to put the keyboard away short of the
                    // system home indicator once it was up.
                    .scrollDismissesKeyboard(.interactively)
                    // anchor: .center (not .bottom) — a new bubble landing
                    // flush against the bottom edge, right on top of the
                    // input bar, read as abrupt/cramped. Centering it in
                    // the visible area instead gives it some breathing
                    // room on both sides, the same settle-into-view feel
                    // most chat apps use rather than a hard snap to the
                    // very bottom.
                    .onChange(of: viewModel.messages.count) { _, _ in
                        if let last = viewModel.messages.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .center) }
                        }
                    }
                }

                inputBar
            }
            .cavnarModuleBackground()
            .navigationBarTitleDisplayMode(.inline)
            .toolbar(.hidden, for: .navigationBar)
            .keyboardDoneToolbar { inputFocused = false }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            GlowBadge(systemImage: "sparkles", size: 40)
            VStack(alignment: .leading, spacing: 2) {
                Text("Ask Cavnar AI")
                    .font(.cavnarHeadline(20.5))
                    .foregroundStyle(Color.cavnarInk)
                Text("Your restaurant intelligence consultant")
                    .font(.cavnarBody(14.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
            Spacer()
            // Moved here from a .keyboard-placement toolbar item — that
            // accessory row floats directly above the system keyboard, the
            // exact same strip of screen the custom input bar's own send
            // button sits in just above the keyboard's safe-area inset, so
            // the two visibly overlapped. A fixed top-right button has no
            // such collision, and only appears while there's actually a
            // keyboard up to dismiss.
            if inputFocused {
                Button("Done") { inputFocused = false }
                    .font(.cavnarBody(15.5, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: inputFocused)
        .padding(.horizontal, 20)
        .padding(.top, 18)
        .padding(.bottom, 14)
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
        }
        .frame(maxWidth: .infinity)
        .padding(.bottom, 20)
    }

    private var inputBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let error = viewModel.errorBanner {
                Text(error)
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarRed)
                    .padding(.horizontal, 4)
            }
            // Only surfaced as the cap approaches — the server truncates at
            // 500 characters silently, so the limit has to be visible before
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
        .padding(.vertical, 12)
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) {
            Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
        }
    }

    private var inputRow: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("How can I help?", text: $viewModel.question, axis: .vertical)
                .font(.cavnarBody(15.5))
                .foregroundStyle(Color.cavnarInk)
                .focused($inputFocused)
                .lineLimit(1...5)
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .background(Color.cavnarPaper2)
                .overlay(
                    RoundedRectangle(cornerRadius: CavnarRadius.sheet)
                        .strokeBorder(inputFocused ? Color.cavnarEmber.opacity(0.5) : Color.white.opacity(0.06), lineWidth: 1)
                )
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.sheet))
                .animation(.easeOut(duration: 0.15), value: inputFocused)

            Button {
                Haptic.light()
                Task { await viewModel.submit() }
            } label: {
                ZStack {
                    Circle().fill(sendButtonFill)
                    Image(systemName: "arrow.up")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundStyle(viewModel.canSubmit ? .white : Color.cavnarInk3)
                }
                .frame(width: 38, height: 38)
                .shadow(color: viewModel.canSubmit ? Color.cavnarEmber.opacity(0.4) : .clear, radius: 8, y: 3)
                // Visual stays 38pt; the hit region meets the 44pt HIG
                // minimum — this is the primary action of the AI screen
                // (audit 7.3).
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
            }
            .disabled(!viewModel.canSubmit)
            .animation(.easeOut(duration: 0.15), value: viewModel.canSubmit)
            .accessibilityLabel("Send question")
            .accessibilityHint(viewModel.canSubmit ? "" : "Type a question first")
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
/// (18pt) — the standard chat-bubble directional cue (iMessage etc.) that
/// the previous uniform-radius rectangle bubbles didn't have.
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
    var onReveal: (() -> Void)? = nil

    // Bubble's outer cap, minus its own horizontal padding (15pt each
    // side) — the width actually available to the text itself.
    private static let maxBubbleWidth: CGFloat = 280
    private static let maxTextWidth: CGFloat = maxBubbleWidth - 30
    /// Measurement font for cavnarMeasuredTextWidth. Falls back to the system
    /// font rather than force-unwrapping: UIFont(name:size:) returns nil if the
    /// custom font fails to register (a renamed .ttf, a PostScript-name
    /// mismatch, iOS failing to register under memory pressure), and this is a
    /// static let on the app's main AI screen — the unwrap crashed the whole
    /// surface (audit 2.1).
    private static let baseTextFont: UIFont =
        UIFont(name: "ApfelGrotezk-Regular", size: 16) ?? .systemFont(ofSize: 16)

    /// Scaled to the user's current text size. Measuring with a frozen 16pt
    /// font while the rendered Text scales with Dynamic Type would under-
    /// measure and clip every bubble (audit 7.1/7.2).
    private static var textFont: UIFont {
        UIFontMetrics(forTextStyle: .body).scaledFont(for: baseTextFont)
    }

    // Fourth attempt at the bubble-hugging bug. The first three all relied
    // on SwiftUI's own implicit sizing (fixedSize, frame(maxWidth:)
    // ordering, Spacer removal) inside this exact nested hierarchy
    // (HStack > VStack > padding > frame(maxWidth:) > background/clipShape)
    // and none of them held — "Yes" kept rendering at the full maxWidth
    // regardless. Rather than guess at a fourth variation on the same
    // technique, this sidesteps SwiftUI's content-hugging negotiation
    // entirely: cavnarMeasuredTextWidth measures the real rendered width via
    // UIKit's NSString.boundingRect, and that exact number is applied
    // directly to the Text/TypewriterText below via frame(width:) — not
    // frame(maxWidth:). There's no longer a "hug vs cap" decision for
    // SwiftUI to get wrong; the width is just a known value. The VStack
    // then sizes itself to the max of its two children (the "CAVNAR AI"
    // label and this exactly-sized text), which is unambiguous, ordinary
    // VStack behavior.
    private var userTextWidth: CGFloat {
        cavnarMeasuredTextWidth(message.text, font: Self.textFont, maxWidth: Self.maxTextWidth)
    }

    var body: some View {
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
                    // Word-by-word reveal — same "AI is composing" language
                    // as AIConsultantView's insight boxes elsewhere in the
                    // app, instead of the answer just snapping in instantly.
                    TypewriterText(
                        fullText: message.text, font: .cavnarBody(16), color: Color.cavnarInk, lineSpacing: 5,
                        maxWidth: Self.maxTextWidth, measuringFont: Self.textFont,
                        onReveal: onReveal
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
                }
            }
            .padding(.horizontal, 15)
            .padding(.vertical, 12)
            .background(bubbleBackground)
            .clipShape(chatBubbleShape(isUser: message.isUser))
            .overlay(
                chatBubbleShape(isUser: message.isUser)
                    .strokeBorder(message.isUser ? Color.white.opacity(0.15) : Color.white.opacity(0.06), lineWidth: 1)
            )
            .shadow(color: message.isUser ? Color.cavnarEmber.opacity(0.22) : Color.black.opacity(0.25), radius: 10, x: 0, y: 4)
        }
        .frame(maxWidth: .infinity, alignment: message.isUser ? .trailing : .leading)
    }

    private var bubbleBackground: AnyShapeStyle {
        message.isUser
            ? AnyShapeStyle(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber], startPoint: .topLeading, endPoint: .bottomTrailing))
            : AnyShapeStyle(Color.cavnarPaper2)
    }
}

// Matches the app's standard sliding-pulse skeleton (CavnarSkeletonLines)
// instead of a spinner — same loading language as everywhere else, and the
// two short bars read as "Cavnar AI is composing a short answer" rather
// than an indeterminate spin.
private struct LoadingBubble: View {
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            GlowBadge(systemImage: "sparkles", size: 28)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 10) {
                Text("CAVNAR AI")
                    .font(.cavnarBody(13.5, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                // "Composing" — an ember caret writing lines into place
                // while Cavnar thinks (see CavnarMotion).
                CavnarComposingLines(widths: [0.8, 0.45, 0.65], lineHeight: 8, spacing: 8)
                    .frame(width: 150)
            }
            .padding(.horizontal, 15)
            .padding(.vertical, 12)
            .background(Color.cavnarPaper2)
            .clipShape(chatBubbleShape(isUser: false))
            .overlay(
                chatBubbleShape(isUser: false)
                    .strokeBorder(Color.white.opacity(0.06), lineWidth: 1)
            )
        }
        // Same edge-positioning approach as ChatBubble, for consistency —
        // see its comment for why this replaced a trailing Spacer.
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
