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
    /// The chat whose Delete is asking first (M6).
    @State private var deleting: AskConversation?

    var body: some View {
        Group {
            if viewModel.isLoadingConversations && viewModel.conversations.isEmpty {
                CavnarLoadingOrb()
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
                .cavnarText(.headline)
            Text("Every conversation you have with Cavnar AI is kept here, so you can pick one back up later.")
                .cavnarText(.body)
                .multilineTextAlignment(.center)
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
                    // Permanent — no undo — so it asks first (iOS re-audit
                    // M6): no full swipe, and the swipe's Delete opens a
                    // confirm naming the chat.
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button(role: .destructive) {
                            Haptic.light()
                            deleting = conversation
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
                CavnarKicker("\(viewModel.conversations.count) \(viewModel.conversations.count == 1 ? "conversation" : "conversations")")
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
        .confirmationDialog(deleting.map { "Delete \u{201C}\($0.title)\u{201D}?" } ?? "",
                            isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                            titleVisibility: .visible, presenting: deleting) { conversation in
            Button("Delete chat", role: .destructive) {
                Task {
                    if await !viewModel.delete(conversation) { deleteFailed = true }
                }
            }
            Button("Keep it", role: .cancel) {}
        } message: { _ in
            Text("This chat is removed for good. It can\u{2019}t be undone.")
        }
    }
}

private struct ConversationRow: View {
    let conversation: AskConversation
    let isOpen: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(conversation.title)
                    .cavnarText(.label)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 6)
                if isOpen {
                    AccountPill(text: "Open")
                }
            }
            if !conversation.preview.isEmpty {
                Text(conversation.preview)
                    .cavnarText(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                HomeMixedText.make(AccountRelativeTime.describe(conversation.updatedAt)
                                   + " \u{00B7} \(conversation.messageCount) \(conversation.messageCount == 1 ? "message" : "messages")",
                                   role: .caption)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber)
            }
            .foregroundStyle(Color.cavnarInk3)
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
