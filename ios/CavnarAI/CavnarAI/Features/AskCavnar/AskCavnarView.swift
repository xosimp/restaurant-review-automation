import SwiftUI

struct AskCavnarView: View {
    @State private var viewModel = AskCavnarViewModel()
    @FocusState private var inputFocused: Bool

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
                                ChatBubble(message: message).id(message.id)
                            }
                            if viewModel.isLoading {
                                LoadingBubble()
                            }
                        }
                        .padding(16)
                    }
                    // Drag the message list and the keyboard tracks/dismisses
                    // with the gesture, same as Messages/Mail — previously
                    // there was no way to put the keyboard away short of the
                    // system home indicator once it was up.
                    .scrollDismissesKeyboard(.interactively)
                    .onChange(of: viewModel.messages.count) { _, _ in
                        if let last = viewModel.messages.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                        }
                    }
                }

                inputBar
            }
            .cavnarModuleBackground()
            .navigationBarTitleDisplayMode(.inline)
            .toolbar(.hidden, for: .navigationBar)
        }
        .cavnarSheetTopRim()
    }

    private var header: some View {
        HStack(spacing: 12) {
            GlowBadge(systemImage: "sparkles", size: 40)
            VStack(alignment: .leading, spacing: 2) {
                Text("Ask Cavnar AI")
                    .font(.cavnarHeadline(19))
                    .foregroundStyle(Color.cavnarInk)
                Text("Your restaurant intelligence consultant")
                    .font(.cavnarBody(11))
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
                    .font(.cavnarBody(14, weight: 600))
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
                    .font(.cavnarHeadline(20))
                    .foregroundStyle(Color.cavnarInk)
                Text("Your numbers, or general advice on running the place — I'll pull in your real data whenever it's relevant.")
                    .font(.cavnarBody(13))
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
                                .font(.system(size: 11, weight: .semibold))
                            Text(question)
                                .font(.cavnarBody(13, weight: 600))
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
        HStack(alignment: .bottom, spacing: 10) {
            TextField("How can I help?", text: $viewModel.question, axis: .vertical)
                .font(.cavnarBody(14))
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
            }
            .disabled(!viewModel.canSubmit)
            .animation(.easeOut(duration: 0.15), value: viewModel.canSubmit)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) {
            Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
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

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if message.isUser {
                Spacer(minLength: 40)
            } else {
                GlowBadge(systemImage: "sparkles", size: 28)
                    .padding(.top, 2)
            }

            VStack(alignment: .leading, spacing: 6) {
                if !message.isUser {
                    Text("CAVNAR AI")
                        .font(.cavnarBody(9, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                }
                if message.isUser {
                    Text(message.text)
                        .font(.cavnarBody(14))
                        .lineSpacing(5)
                        .foregroundStyle(.white)
                        // Same fix as TypewriterText: without this, a short
                        // message like "Yes" was rendering in a bubble
                        // stretched to the full 270pt max width instead of
                        // hugging its actual content — real bug, reported
                        // live from a screenshot.
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    // Word-by-word reveal — same "AI is composing" language
                    // as AIConsultantView's insight boxes elsewhere in the
                    // app, instead of the answer just snapping in instantly.
                    TypewriterText(fullText: message.text, font: .cavnarBody(14), color: Color.cavnarInk, lineSpacing: 5)
                }
            }
            .padding(.horizontal, 15)
            .padding(.vertical, 12)
            .frame(maxWidth: 270, alignment: .leading)
            .background(bubbleBackground)
            .clipShape(chatBubbleShape(isUser: message.isUser))
            .overlay(
                chatBubbleShape(isUser: message.isUser)
                    .strokeBorder(message.isUser ? Color.white.opacity(0.15) : Color.white.opacity(0.06), lineWidth: 1)
            )
            .shadow(color: message.isUser ? Color.cavnarEmber.opacity(0.22) : Color.black.opacity(0.25), radius: 10, x: 0, y: 4)

            if !message.isUser {
                Spacer(minLength: 40)
            }
        }
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
                    .font(.cavnarBody(9, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                CavnarSkeletonLines(widths: [0.8, 0.45], lineHeight: 10, spacing: 8)
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
            Spacer(minLength: 40)
        }
    }
}
