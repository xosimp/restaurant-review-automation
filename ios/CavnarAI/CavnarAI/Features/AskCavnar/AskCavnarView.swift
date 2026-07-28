import SwiftUI

struct AskCavnarView: View {
    @State private var viewModel = AskCavnarViewModel()
    @FocusState private var inputFocused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 12) {
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
                        .clipShape(RoundedRectangle(cornerRadius: 10))
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
            .navigationTitle("Ask Cavnar")
        }
    }
}

private struct ChatBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.isUser { Spacer(minLength: 40) }
            Text(message.text)
                .font(.cavnarBody(14))
                .foregroundStyle(message.isUser ? .white : Color.cavnarInk)
                .padding(12)
                .background(message.isUser ? Color.cavnarEmber : Color.cavnarPaper2)
                .clipShape(RoundedRectangle(cornerRadius: 14))
            if !message.isUser { Spacer(minLength: 40) }
        }
    }
}

private struct LoadingBubble: View {
    var body: some View {
        HStack {
            ProgressView()
                .padding(12)
                .background(Color.cavnarPaper2)
                .clipShape(RoundedRectangle(cornerRadius: 14))
            Spacer(minLength: 40)
        }
    }
}
