import SwiftUI

/// Every past conversation with Cavnar AI, newest first. Tap one to pick
/// it back up in the chat; swipe it away to delete it for good. Pushed
/// from the Ask Cavnar tab's header.
///
/// A `List`, not the app's usual ScrollView + card stack — swipe-to-delete
/// is a List feature, and the rows are restyled to match the Account
/// cards so it doesn't read as a different app.
struct AskCavnarHistoryView: View {
    let viewModel: AskCavnarViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var deleteFailed = false

    var body: some View {
        Group {
            if viewModel.isLoadingConversations && viewModel.conversations.isEmpty {
                CavnarLoadingSeal()
                    .padding(.top, 60)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            } else if viewModel.conversations.isEmpty {
                empty
            } else {
                list
            }
        }
        .cavnarModuleBackground()
        .navigationTitle("Chat History")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Chat History") }
        .cavnarEmberBackButton()
        .task { await viewModel.refreshConversations() }
        .alert("Couldn't delete that chat", isPresented: $deleteFailed) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("Check your connection and try again.")
        }
    }

    private var empty: some View {
        VStack(spacing: 10) {
            Image(systemName: "bubble.left.and.text.bubble.right")
                .font(.system(size: 30, weight: .medium))
                .foregroundStyle(Color.cavnarEmber.opacity(0.7))
                .padding(.bottom, 4)
            Text("No chats yet")
                .font(.cavnarHeadline(19))
                .foregroundStyle(Color.cavnarInk)
            Text("Every conversation you have with Cavnar AI is kept here, so you can pick one back up later.")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
                .lineSpacing(3)
                .padding(.horizontal, 32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .padding(.top, 70)
    }

    private var list: some View {
        List {
            Section {
                ForEach(viewModel.conversations) { conversation in
                    Button {
                        Haptic.light()
                        Task {
                            await viewModel.open(conversation)
                            dismiss()
                        }
                    } label: {
                        ConversationRow(conversation: conversation,
                                        isOpen: conversation.id == viewModel.conversationId)
                    }
                    .buttonStyle(.plain)
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
                    .listRowInsets(EdgeInsets(top: 5, leading: 20, bottom: 5, trailing: 20))
                    // Permanent — no undo. A full swipe commits, same as
                    // Mail/Messages; the row leaves immediately and only
                    // comes back if the server refuses.
                    .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                        Button(role: .destructive) {
                            Haptic.light()
                            Task {
                                if await !viewModel.delete(conversation) { deleteFailed = true }
                            }
                        } label: {
                            Label("Delete", systemImage: "trash")
                        }
                        // No explicit tint — inherits the app-wide ember
                        // tint (RootView), same as Schedule History's
                        // identical delete swipe. An explicit .tint(.cavnarRed)
                        // here used to override that with system red, so
                        // this was the one delete swipe in the app that
                        // didn't match the other's orange.
                    }
                }
            } header: {
                Text("\(viewModel.conversations.count) \(viewModel.conversations.count == 1 ? "conversation" : "conversations") · swipe left to delete")
                    .font(.cavnarBody(13, weight: 700))
                    .tracking(1.2)
                    .textCase(.uppercase)
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.bottom, 2)
                    // Matches the rows' own leading inset (20, set below)
                    // exactly, rather than relying on the list style's
                    // own default header inset — that default was what
                    // left this sitting to the left of the row cards.
                    .listRowInsets(EdgeInsets(top: 0, leading: 20, bottom: 8, trailing: 20))
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .animation(.easeOut(duration: 0.2), value: viewModel.conversations)
    }
}

private struct ConversationRow: View {
    let conversation: AskConversation
    let isOpen: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(conversation.title)
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                Spacer(minLength: 6)
                if isOpen {
                    AccountPill(text: "Open")
                }
            }
            if !conversation.preview.isEmpty {
                Text(conversation.preview)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                Text(AccountRelativeTime.describe(conversation.updatedAt))
                    .font(.cavnarNumber(13))
                Text("·").font(.cavnarBody(13))
                Text("\(conversation.messageCount) \(conversation.messageCount == 1 ? "message" : "messages")")
                    .font(.cavnarNumber(13))
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber)
            }
            .foregroundStyle(Color.cavnarInk3.opacity(0.85))
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2.opacity(0.6))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(isOpen ? Color.cavnarEmber.opacity(0.45) : Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens this conversation")
    }
}
