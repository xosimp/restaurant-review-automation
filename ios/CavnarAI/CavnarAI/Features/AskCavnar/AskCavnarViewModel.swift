import Foundation
import Observation

struct ChatMessage: Identifiable {
    let id = UUID()
    let text: String
    let isUser: Bool
    /// True when the model hit max_tokens — the answer stops mid-thought and
    /// must not be presented as a finished one (audit 5.1).
    var wasTruncated: Bool = false
    /// Actions the assistant wants to take and deliberately cannot take
    /// itself. Rendered as confirm cards under the answer; nothing happens
    /// until the owner taps one.
    var proposals: [AskProposal] = []
    /// Set once this message's typewriter reveal has actually played. The
    /// view model (not the view) owns this because the view's own @State is
    /// torn down every time the screen goes away — without a model-level
    /// flag, leaving and coming back replayed every answer's typing
    /// animation from scratch, every time.
    var hasRevealed: Bool = false

    /// The backend records what the owner did with a proposal as a
    /// "[Confirmed: …]" / "[Dismissed: …]" user turn (so the model knows).
    /// In the transcript that's a status line, not something the owner
    /// typed — rendered as a small centered note instead of a bubble.
    var isStatusLine: Bool {
        isUser && (text.hasPrefix("[Confirmed:") || text.hasPrefix("[Dismissed:"))
    }

    /// "[Confirmed: Email Fresh Co]" → ("Confirmed", "Email Fresh Co").
    var statusParts: (verb: String, label: String)? {
        guard isStatusLine, let colon = text.firstIndex(of: ":") else { return nil }
        let verb = String(text[text.index(after: text.startIndex)..<colon])
        var label = String(text[text.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
        if label.hasSuffix("]") { label.removeLast() }
        return (verb, label)
    }
}

/// A proposed action. `route` is the same authenticated endpoint the app's
/// own button uses, so confirming can never reach anything the user could
/// not already do themselves.
struct AskProposal: Decodable, Identifiable, Hashable {
    let action: String
    let summary: String
    let route: Route
    let body: [String: AnyCodableValue]?

    var id: String { action + summary }

    struct Route: Decodable, Hashable {
        let mobile: String
        let method: String
    }
}

/// One row in the chat history: a past conversation the owner can reopen
/// or delete.
struct AskConversation: Decodable, Identifiable, Hashable {
    let id: Int
    let title: String
    let preview: String
    let messageCount: Int
    let updatedAt: String
    let createdAt: String

    enum CodingKeys: String, CodingKey {
        case id, title, preview
        case messageCount = "message_count"
        case updatedAt = "updated_at"
        case createdAt = "created_at"
    }
}

/// Minimal JSON value box — proposal bodies are small and untyped
/// (a supplier email, a schedule id), and this avoids inventing a
/// separate Swift type per action.
enum AnyCodableValue: Decodable, Hashable, Encodable {
    case string(String), int(Int), double(Double), bool(Bool), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Int.self) { self = .int(v) }
        else if let v = try? c.decode(Double.self) { self = .double(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else { self = .null }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .int(let v):    try c.encode(v)
        case .double(let v): try c.encode(v)
        case .bool(let v):   try c.encode(v)
        case .null:          try c.encodeNil()
        }
    }
}

/// Real multi-turn chat, persisted server-side as conversations. The
/// backend replays the current conversation's stored turns to the model
/// on every question, so a short follow-up like "yes" resolves against
/// what was actually being discussed — and the same chat is there again
/// after an app restart, in the history list, next to every earlier one.
@Observable
@MainActor
final class AskCavnarViewModel {
    /// Mirrors ask_cavnar.py's `_MAX_HISTORY_MESSAGES`. Anything older is
    /// discarded server-side anyway; sending it only cost upload time on the
    /// weak connections this app runs on (audit 5.4).
    static let maxHistoryMessages = 12
    /// Mirrors ask_cavnar.py's `_MAX_QUESTION_LENGTH`. Enforced here so the
    /// user sees the limit rather than having the tail of their question
    /// silently cut server-side (audit 5.2).
    static let maxQuestionLength = 2000
    /// Mirrors models._ASK_TRANSCRIPT_KEEP — the most one chat holds.
    static let maxLoadedMessages = 200

    var messages: [ChatMessage] = [] {
        didSet {
            if messages.count > Self.maxLoadedMessages {
                messages.removeFirst(messages.count - Self.maxLoadedMessages)
            }
        }
    }

    var question = "" {
        didSet {
            if question.count > Self.maxQuestionLength {
                question = String(question.prefix(Self.maxQuestionLength))
            }
        }
    }

    var isLoading = false
    /// What the assistant is doing right now — "Reading your reviews" — shown
    /// while a multi-tool answer is in flight. nil outside a request, and
    /// while the stream connects, so it never flashes stale text left over
    /// from the previous question.
    var statusLabel: String?
    /// The orb's motion for the current moment, from the stream's `state`
    /// field. `.connecting` from the instant a question is sent until the
    /// first progress event arrives, so the orb never sits still.
    var orbState: CavnarOrbState = .connecting
    /// Transient failure notice shown above the input bar. Deliberately not a
    /// ChatMessage: an error appended as an assistant turn ends up replayed to
    /// Claude in `history` as something it supposedly said (audit 2.5).
    var errorBanner: String?

    // MARK: Conversations

    /// The chat on screen. nil until the first answer of a brand-new chat
    /// comes back with its id, or while there are no chats at all.
    var conversationId: Int?
    /// True after "New chat" until the first question is answered — tells
    /// the backend to open a fresh conversation for that question instead
    /// of appending to the current one. Created server-side on the first
    /// question, so an abandoned New chat never leaves an empty row.
    private var wantsNewConversation = false
    var conversations: [AskConversation] = []
    var isLoadingConversations = false
    /// True while the transcript of a past chat is being fetched to reopen.
    var isOpeningConversation = false
    private var hasLoadedInitial = false

    var remainingCharacters: Int { Self.maxQuestionLength - question.count }

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    var canSubmit: Bool {
        !question.trimmingCharacters(in: .whitespaces).isEmpty && !isLoading && !isOpeningConversation
    }

    private struct HistoryTurn: Encodable {
        let role: String
        let content: String
    }

    private struct AskBody: Encodable {
        let question: String
        let history: [HistoryTurn]
        let conversation_id: Int?
        let new_conversation: Bool
    }

    private struct StreamBody: Encodable {
        let question: String
        let conversation_id: Int?
        let new_conversation: Bool
    }

    private struct AskResponse: Decodable {
        let ok: Bool
        let answer: String?
        let error: String?
        let truncated: Bool?
        let proposals: [AskProposal]?
        let conversationId: Int?

        enum CodingKeys: String, CodingKey {
            case ok, answer, error, truncated, proposals
            case conversationId = "conversation_id"
        }
    }

    private struct ActionOutcomeBody: Encodable {
        let action: String
        let outcome: String
        let summary: String
        let conversation_id: Int?
    }

    private struct PlainOK: Decodable { let ok: Bool; let error: String? }

    private struct ConversationsResponse: Decodable {
        let ok: Bool
        let conversations: [AskConversation]?
    }

    private struct StoredMessage: Decodable {
        let role: String
        let content: String
        let proposals: [AskProposal]?
    }

    private struct ConversationResponse: Decodable {
        let ok: Bool
        let messages: [StoredMessage]?
    }

    // MARK: Chat history

    /// First appearance of the tab: pick up where the owner left off — the
    /// most recent chat, already fully revealed (no retyping of old
    /// answers). Nothing to do on later appearances; the tab keeps state.
    func loadInitialIfNeeded() async {
        guard !hasLoadedInitial else { return }
        hasLoadedInitial = true
        await refreshConversations()
        if let latest = conversations.first, messages.isEmpty, conversationId == nil {
            await open(latest)
        }
    }

    func refreshConversations() async {
        isLoadingConversations = true
        defer { isLoadingConversations = false }
        if let response: ConversationsResponse = try? await client.send(
            "/mobile/api/ask-cavnar/conversations", hapticOnError: false),
           response.ok {
            conversations = response.conversations ?? []
        }
    }

    /// Reopens a past chat. Old proposals are deliberately NOT rendered as
    /// live confirm cards again — a supplier order the owner already sent
    /// last week must not come back with a working Confirm button. The
    /// "[Confirmed: …]" status lines in the transcript show what happened.
    func open(_ conversation: AskConversation) async {
        guard !isLoading else { return }
        isOpeningConversation = true
        defer { isOpeningConversation = false }
        do {
            let response: ConversationResponse = try await client.send(
                "/mobile/api/ask-cavnar/conversations/\(conversation.id)", hapticOnError: false)
            guard response.ok else { return }
            let stored = response.messages ?? []
            messages = stored.map { m in
                ChatMessage(text: m.content, isUser: m.role == "user", hasRevealed: true)
            }
            conversationId = conversation.id
            wantsNewConversation = false
            errorBanner = nil
        } catch {
            errorBanner = "Couldn't open that chat — check your connection and try again."
        }
    }

    /// A fresh conversation. Nothing is created until the first question
    /// is answered, so backing out costs nothing.
    func startNewChat() {
        guard !isLoading else { return }
        messages = []
        conversationId = nil
        wantsNewConversation = true
        errorBanner = nil
        statusLabel = nil
    }

    /// Permanent. If it was the chat on screen, the screen becomes a new
    /// chat — the deleted transcript never lingers.
    func delete(_ conversation: AskConversation) async -> Bool {
        let wasOpen = conversation.id == conversationId
        // Drop it from the list first so the swipe-to-delete row leaves
        // immediately; put it back if the server refuses.
        let previous = conversations
        conversations.removeAll { $0.id == conversation.id }
        do {
            let response: PlainOK = try await client.send(
                "/mobile/api/ask-cavnar/conversations/\(conversation.id)", method: .delete)
            guard response.ok else { conversations = previous; return false }
        } catch {
            conversations = previous
            return false
        }
        if wasOpen { startNewChat() }
        return true
    }

    // MARK: Proposals

    /// Fire a confirmed proposal. Runs the app's own endpoint, then writes
    /// the audit line — never the other way round, so a recorded
    /// "confirmed" always means it really ran.
    func confirm(_ proposal: AskProposal) async -> Bool {
        do {
            let response: PlainOK
            if proposal.route.method == "GET" {
                response = try await client.send(proposal.route.mobile)
            } else {
                response = try await client.send(proposal.route.mobile, method: .post,
                                                 body: proposal.body ?? [:])
            }
            if response.ok { await record(proposal, outcome: "confirmed") }
            return response.ok
        } catch {
            return false
        }
    }

    func dismiss(_ proposal: AskProposal) async {
        await record(proposal, outcome: "dismissed")
    }

    private func record(_ proposal: AskProposal, outcome: String) async {
        let _: PlainOK? = try? await client.send(
            "/mobile/api/ask-cavnar/action", method: .post,
            body: ActionOutcomeBody(action: proposal.action, outcome: outcome,
                                    summary: proposal.summary, conversation_id: conversationId))
        // The status line the backend just wrote — mirrored locally so the
        // transcript on screen matches what a reopen would show.
        let verb = outcome == "confirmed" ? "Confirmed" : "Dismissed"
        messages.append(ChatMessage(text: "[\(verb): \(proposal.summary)]", isUser: true, hasRevealed: true))
    }

    /// Marks an answer as having already played its typewriter reveal, so
    /// TypewriterText renders it instantly next time instead of retyping it.
    func markRevealed(_ id: UUID) {
        if let idx = messages.firstIndex(where: { $0.id == id }) {
            messages[idx].hasRevealed = true
        }
    }

    // MARK: Asking

    func submit() async {
        guard canSubmit else { return }
        let asked = question
        errorBanner = nil
        // Captured from `messages` BEFORE appending the new question, so
        // this is exactly the prior back-and-forth — the backend appends
        // `question` itself as the final turn, matching ask_cavnar.py's
        // own `history` contract (prior turns only, not including the new
        // question). Only the plain-request fallback sends it; the stream
        // route replays the stored conversation instead.
        let history = messages
            .filter { !$0.isStatusLine }
            .suffix(Self.maxHistoryMessages)
            .map { HistoryTurn(role: $0.isUser ? "user" : "assistant", content: String($0.text.prefix(800))) }
        messages.append(ChatMessage(text: asked, isUser: true))
        question = ""
        isLoading = true
        statusLabel = nil
        orbState = .connecting
        defer { isLoading = false; statusLabel = nil; orbState = .connecting }

        do {
            try await streamAnswer(for: asked)
        } catch is CancellationError {
            // The screen went away mid-request — roll the turn back silently.
            if messages.last?.isUser == true { messages.removeLast() }
            question = asked
        } catch {
            // Streaming failed before any answer arrived (connection refused,
            // a proxy stripping the response, decode failure on the first
            // line) — fall back to the plain request rather than surface a
            // dead end. A failure partway through, after some progress
            // already rendered, is not retried here: the loop already ran
            // real tool calls server-side, and re-running it would risk a
            // second attempt at the same write-tool proposal.
            do {
                let response: AskResponse = try await client.send(
                    "/mobile/api/ask-cavnar", method: .post,
                    body: AskBody(question: asked, history: history,
                                  conversation_id: conversationId, new_conversation: wantsNewConversation)
                )
                if response.ok { adopt(conversationId: response.conversationId) }
                appendAnswer(from: response.ok ? (response.answer ?? "") : (response.error ?? "Something went wrong."),
                            truncated: response.truncated == true, proposals: response.proposals ?? [])
            } catch is CancellationError {
                if messages.last?.isUser == true { messages.removeLast() }
                question = asked
            } catch {
                // Roll the turn back rather than leaving a dead end: the
                // question returns to the input box ready to resend (it used
                // to be cleared and lost, making "try again" harder to follow
                // than it sounds), and the failure never enters `history`
                // (audit 2.5).
                if messages.last?.isUser == true { messages.removeLast() }
                question = asked
                errorBanner = (error as? APIClient.APIError)?.message
                    ?? "Couldn't reach Cavnar AI — check your connection and try again."
            }
        }
    }

    /// Consumes the SSE stream: "progress" updates statusLabel as each tool
    /// runs, "answer" appends the final message, "error" surfaces the
    /// server's own message. Mirrors the web client's fetch/ReadableStream
    /// loop — same events, same fallback-on-failure shape.
    private func streamAnswer(for question: String) async throws {
        var gotAnswer = false
        for try await event in await client.stream(
            "/mobile/api/ask-cavnar/stream",
            body: StreamBody(question: question, conversation_id: conversationId,
                             new_conversation: wantsNewConversation)) {
            switch event.type {
            case "progress":
                statusLabel = event.label
                if let raw = event.state, let mapped = CavnarOrbState(rawValue: raw) {
                    orbState = mapped
                }
            case "answer":
                gotAnswer = true
                adopt(conversationId: event.conversationId)
                appendAnswer(from: event.answer ?? "", truncated: event.truncated == true,
                            proposals: event.proposals ?? [])
            case "error":
                gotAnswer = true
                appendAnswer(from: event.error ?? "Something went wrong.", truncated: false, proposals: [])
            default:
                break
            }
        }
        if !gotAnswer {
            // The stream closed with no "answer"/"error" event at all — a
            // proxy that buffers/drops SSE, most likely. Let the caller's
            // catch block run the plain-request fallback.
            throw APIClient.APIError(message: "Stream ended without an answer.")
        }
    }

    /// An answered question settles which chat we're in: a New chat now
    /// has an id, and the history list needs the new/updated row.
    private func adopt(conversationId id: Int?) {
        if let id { conversationId = id }
        wantsNewConversation = false
        Task { await refreshConversations() }
    }

    private func appendAnswer(from raw: String, truncated: Bool, proposals: [AskProposal]) {
        let cleaned = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let display = cleaned.isEmpty
            ? "I didn't get an answer back that time — mind asking again?"
            : cleaned
        messages.append(ChatMessage(text: display, isUser: false, wasTruncated: truncated, proposals: proposals))
    }
}
