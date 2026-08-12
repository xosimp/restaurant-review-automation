import SwiftUI

struct AskCavnarView: View {
    @State private var viewModel = AskCavnarViewModel()
    @FocusState private var inputFocused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 18) {
                            if viewModel.messages.isEmpty {
                                Text("Ask a quick question about your reviews, labor, food cost, or marketing.")
                                    .font(.cavnarBody(13))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .padding(.top, 40)
                                    .frame(maxWidth: .infinity)
                                    .multilineTextAlignment(.center)
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
                    .onChange(of: viewModel.messages.count) { _, _ in
                        if let last = viewModel.messages.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                        }
                    }
                }

                Divider()

                HStack(spacing: 10) {
                    TextField("Ask Cavnar…", text: $viewModel.question, axis: .vertical)
                        .font(.cavnarBody(14))
                        .focused($inputFocused)
                        .padding(10)
                        .background(Color.cavnarPaper2)
                        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    Button {
                        Task { await viewModel.submit() }
                    } label: {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 30))
                            .foregroundStyle(viewModel.canSubmit ? Color.cavnarEmber : Color.cavnarPaper3)
                    }
                    .disabled(!viewModel.canSubmit)
                }
                .padding(12)
            }
            .background(Color.cavnarPaper)
            .navigationTitle("Ask Cavnar AI")
        }
    }
}

/// Small "who's talking" label above an AI answer — a bare paragraph
/// dropped straight into a bubble with no other structure was reading as
/// one dense wall of text; this plus the wider padding/line-spacing below
/// gives it a clear top edge to start reading from and a consistent voice
/// distinct from the user's own messages.
private struct AICavnarLabel: View {
    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: "sparkles")
                .font(.system(size: 10, weight: .semibold))
            Text("CAVNAR AI")
                .font(.cavnarBody(10, weight: 700))
                .tracking(1)
        }
        .foregroundStyle(Color.cavnarEmber)
    }
}

private struct ChatBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.isUser { Spacer(minLength: 40) }
            VStack(alignment: .leading, spacing: 8) {
                if !message.isUser { AICavnarLabel() }
                Text(message.text)
                    .font(.cavnarBody(14))
                    .lineSpacing(5)
                    .foregroundStyle(Color.cavnarInk)
            }
            .padding(16)
            // Caps the line length instead of letting a short 2-4 sentence
            // answer stretch edge to edge — a narrower column is what
            // actually fixed the "big long paragraph" read, not the prose
            // itself (the backend prompt already caps it to 2-4 sentences).
            .frame(maxWidth: 280, alignment: .leading)
            .background(message.isUser ? Color.cavnarEmber : Color.cavnarPaper2)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
            if !message.isUser { Spacer(minLength: 40) }
        }
    }
}

// Matches the app's standard sliding-pulse skeleton (CavnarSkeletonLines)
// instead of a spinner — same loading language as everywhere else, and the
// two short bars read as "Cavnar AI is composing a short answer" rather
// than an indeterminate spin.
private struct LoadingBubble: View {
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 10) {
                AICavnarLabel()
                CavnarSkeletonLines(widths: [0.8, 0.45], lineHeight: 10, spacing: 8)
                    .frame(width: 160)
            }
            .padding(16)
            .background(Color.cavnarPaper2)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
            Spacer(minLength: 40)
        }
    }
}
