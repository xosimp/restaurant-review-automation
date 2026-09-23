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
    /// What this answer rests on — which modules it consulted, how confident
    /// it is, and any figure in it that could not be traced back to data the
    /// model was handed. Nil on the owner's own turns and on older stored
    /// messages, which predate the backend sending it.
    var evidence: AskEvidence?
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

/// The provenance of one answer.
///
/// Before this the API returned the answer as a bare markdown string, so the
/// app could not show which modules an answer came from or flag a number
/// that did not check out — however carefully the backend had worked both
/// out. `unverifiedFigures` is the important one: it means a currency or
/// percentage in the text does not appear in anything the model was given,
/// and the owner is about to act on it.
struct AskEvidence: Decodable, Hashable {
    var modules: [String] = []
    var confidence: String = "unknown"
    var unverifiedFigures: [String] = []

    /// Nothing worth drawing a strip for.
    var isEmpty: Bool {
        modules.isEmpty && unverifiedFigures.isEmpty && confidence != "low"
    }

    var confidenceLabel: String? {
        confidence == "unknown" ? nil : "\(confidence) confidence"
    }

    var warning: String? {
        guard !unverifiedFigures.isEmpty else { return nil }
        let list = unverifiedFigures.joined(separator: ", ")
        let noun = unverifiedFigures.count > 1 ? "those figures" : "that figure"
        return "Couldn’t verify \(list) against your data — treat \(noun) as unconfirmed."
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
    /// The ask_cavnar_actions row this card is. Answers are recorded against
    /// it, so confirming one supplier order never settles another with the
    /// same action name. Nil from an older backend.
    let proposalId: Int?
    /// What the owner is agreeing to beyond the one-line summary: the money,
    /// who it goes to, the words that would go out.
    let details: [Detail]?
    let preview: String?
    let atStake: Double?

    var id: String { proposalId.map { "p\($0)" } ?? (action + summary) }

    struct Route: Decodable, Hashable {
        let mobile: String
        let method: String
    }

    struct Detail: Decodable, Hashable {
        let label: String
        let value: String
    }

    enum CodingKeys: String, CodingKey {
        case action, summary, route, body, details, preview
        case proposalId = "proposal_id"
        case atStake = "at_stake"
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
    /// Mirrors ask_cavnar.py's `_MAX_HISTORY_TURN_LENGTH`.
    static let maxHistoryTurnLength = 2400
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
    var statusLabel: String? {
        didSet {
            // The reasoning trail: the label that was running is ticked as
            // the next one starts. Every entry is the server's own progress
            // event, in the order the tool loop ran it — never a script.
            if let old = oldValue, !old.isEmpty, old != statusLabel, statusLabel != nil {
                progressTrail.append(old)
            }
        }
    }
    /// Labels already completed this turn, oldest first. Cleared with
    /// statusLabel at the end of a request.
    var progressTrail: [String] = []
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
        let modulesConsulted: [String]?
        let confidence: String?
        let unverifiedFigures: [String]?

        enum CodingKeys: String, CodingKey {
            case ok, answer, error, truncated, proposals, confidence
            case conversationId = "conversation_id"
            case modulesConsulted = "modules_consulted"
            case unverifiedFigures = "unverified_figures"
        }

        var evidence: AskEvidence {
            AskEvidence(modules: modulesConsulted ?? [],
                        confidence: confidence ?? "unknown",
                        unverifiedFigures: unverifiedFigures ?? [])
        }
    }

    private struct ActionOutcomeBody: Encodable {
        let action: String
        let outcome: String
        let summary: String
        let conversation_id: Int?
        let proposal_id: Int?
        let reason: String?
    }

    private typealias PlainOK = APIClient.OKResponse

    /// A confirmed action's response. Most routes do the work inline and
    /// just answer ok; schedule generation hands back a job id and
    /// finishes on a background thread (see confirm()).
    private struct JobOrOK: Decodable {
        let ok: Bool
        let error: String?
        let jobId: String?

        enum CodingKeys: String, CodingKey {
            case ok, error
            case jobId = "job_id"
        }
    }

    private struct JobStatus: Decodable {
        let ok: Bool
        let status: String?
        let error: String?
    }

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

    /// First appearance of the tab after a fresh launch or sign-in: start a
    /// new chat, not the most recent one.
    ///
    /// This used to reopen the latest conversation, fully revealed, on the
    /// theory that resuming where the owner left off saved them a tap. In
    /// practice it meant tapping the home row's Ask Cavnar button dropped
    /// them wherever they'd last scrolled inside an old chat — sometimes
    /// mid-conversation — instead of the fresh composer the tap implies.
    /// Old chats are one tap away in History; this only changes what
    /// greets a brand new session.
    func loadInitialIfNeeded() async {
        guard !hasLoadedInitial else { return }
        hasLoadedInitial = true
        await refreshConversations()
        await loadOpening()
    }

    /// What the screen says before the owner types anything.
    ///
    /// It used to be three hardcoded questions, identical to the web's and
    /// never changing — while the platform was already computing
    /// situation-aware ones from real signals and only the Home tab used
    /// them. No model call: this is the first thing an owner sees, so it has
    /// to be instant, and every line is measured rather than written.
    /// Re-fetched once it goes stale, not held for the life of the process.
    /// An owner who answered the review this was warning about and came back
    /// to the tab was otherwise told about it again.
    func loadOpening(force: Bool = false) async {
        if !force, let loaded = openingLoadedAt,
           Date().timeIntervalSince(loaded) < Self.openingTTL { return }
        if let response: AskOpening = try? await client.send(
            "/mobile/api/ask-cavnar/opening", hapticOnError: false), response.ok {
            opening = response
            openingLoadedAt = Date()
        }
    }

    var opening: AskOpening?
    private var openingLoadedAt: Date?
    private static let openingTTL: TimeInterval = 5 * 60

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

    /// Called on a fresh sign-in (see RootView's onChange(of:
    /// sessionStore.isAuthenticated)). This view model is a single @State
    /// on RootView that outlives sign-out/sign-in within the same app
    /// process — the same reason hasShownHomeIntro and selectedTab need
    /// their own reset there — so without this, signing out of one chat and
    /// back in (same account or a different one on a shared device) landed
    /// on whatever conversation and scroll position were left over, and
    /// `hasLoadedInitial` being permanently true meant even the old
    /// resume-latest-chat behavior never got a chance to run again either.
    func reset() {
        hasLoadedInitial = false
        conversations = []
        startNewChat()
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
    ///
    /// A false return always leaves the reason in `errorBanner`, and
    /// `lastConfirmMayHaveRun` says whether the action may have happened
    /// anyway. The bare catch here used to return false with no message,
    /// and the card offered Confirm again as if nothing had happened — after
    /// a timeout on a guest text blast or a supplier order that may already
    /// have gone out, and after a refusal (quiet hours, a send limit) whose
    /// reason the owner then never saw (CLIENT-19).
    func confirm(_ proposal: AskProposal) async -> Bool {
        lastConfirmMayHaveRun = false
        do {
            let response: JobOrOK
            if proposal.route.method == "GET" {
                response = try await client.send(proposal.route.mobile)
            } else {
                response = try await client.send(proposal.route.mobile, method: .post,
                                                 body: proposal.body ?? [:])
            }
            guard response.ok else {
                errorBanner = response.error ?? "That didn't go through — nothing was sent."
                return false
            }
            // Schedule generation answers immediately with a job id and
            // does the actual work on a background thread. Taking that
            // first ok at face value meant the card said "Done" while the
            // schedule was still being built — and stayed saying it even
            // if the job then failed. Wait for the real outcome.
            if let jobId = response.jobId, !(await scheduleJobSucceeded(jobId)) {
                errorBanner = "The schedule didn't finish building. Open Labor to see where it stopped."
                return false
            }
            await record(proposal, outcome: "confirmed")
            return true
        } catch is CancellationError {
            return false
        } catch let error as APIClient.APIError where error.status == nil && error.mayHaveReachedServer {
            // No answer came back, but the request may have arrived and run.
            lastConfirmMayHaveRun = true
            errorBanner = "We lost the connection before this finished, so it may already have gone through. "
                        + "Check before confirming again."
            return false
        } catch let error as APIClient.APIError {
            // The server said no (its own reason), or the request never left.
            errorBanner = error.message
            return false
        } catch {
            errorBanner = error.localizedDescription
            return false
        }
    }

    /// Set by confirm() when its request timed out or dropped mid-flight —
    /// the action may have run even though no answer arrived.
    private(set) var lastConfirmMayHaveRun = false

    /// Polls the async schedule job to its real conclusion. Generation
    /// takes a little while (it's a live model call over the roster), so
    /// this allows a generous window before giving up rather than
    /// reporting a failure the owner would have to guess at.
    private func scheduleJobSucceeded(_ jobId: String) async -> Bool {
        for _ in 0..<40 {
            try? await Task.sleep(for: .seconds(1.5))
            guard let status: JobStatus = try? await client.send(
                "/mobile/api/labor/schedule-status/\(jobId)", hapticOnError: false)
            else { continue }
            if status.status == "pending" { continue }
            return status.ok && status.status != "error"
        }
        return false
    }

    /// "Not now", with the owner's optional reason — Ask reads it before
    /// proposing the same thing again.
    func dismiss(_ proposal: AskProposal, reason: String? = nil) async {
        await record(proposal, outcome: "dismissed", reason: reason)
    }

    private func record(_ proposal: AskProposal, outcome: String, reason: String? = nil) async {
        let why = reason?.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanReason = (why?.isEmpty ?? true) ? nil : String(why!.prefix(300))
        let _: PlainOK? = try? await client.send(
            "/mobile/api/ask-cavnar/action", method: .post,
            body: ActionOutcomeBody(action: proposal.action, outcome: outcome,
                                    summary: proposal.summary, conversation_id: conversationId,
                                    proposal_id: proposal.proposalId, reason: cleanReason))
        // The status line the backend just wrote — mirrored locally so the
        // transcript on screen matches what a reopen would show.
        let verb = outcome == "confirmed" ? "Confirmed" : "Dismissed"
        let tail = cleanReason.map { " — \($0)" } ?? ""
        messages.append(ChatMessage(text: "[\(verb): \(proposal.summary)\(tail)]", isUser: true, hasRevealed: true))
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
            // Mirrors ask_cavnar._MAX_HISTORY_TURN_LENGTH. Raised with it when
            // executive answers got longer: truncating the assistant's own
            // previous turn meant a follow-up like "do the second one"
            // resolved against an answer whose second option had been cut off.
            .map { HistoryTurn(role: $0.isUser ? "user" : "assistant",
                               content: String($0.text.prefix(Self.maxHistoryTurnLength))) }
        messages.append(ChatMessage(text: asked, isUser: true))
        question = ""
        isLoading = true
        statusLabel = nil
        orbState = .connecting
        defer { isLoading = false; statusLabel = nil; progressTrail = []; orbState = .connecting }

        do {
            try await streamAnswer(for: asked)
        } catch is CancellationError {
            // The screen went away mid-request — roll the turn back silently.
            if messages.last?.isUser == true { messages.removeLast() }
            question = asked
        } catch is StreamCutAfterProgress {
            // Tools already ran server-side before the stream died. Asking
            // again through the plain route re-billed the model and every
            // tool it called, and could repeat a write proposal (CLIENT-28).
            // The server may still finish and file the answer, so point the
            // owner at the chat history rather than re-asking on their behalf.
            if messages.last?.isUser == true { messages.removeLast() }
            question = asked
            errorBanner = "The connection dropped while Cavnar AI was working on this. "
                        + "Its answer may still land in your chat history — check there before asking again."
            Task { await refreshConversations() }
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
                            truncated: response.truncated == true, proposals: response.proposals ?? [],
                            evidence: response.ok ? response.evidence : nil)
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
        // Any progress event means the server's loop is running tools on
        // this question; from then on a failure must not trigger a re-ask.
        var sawProgress = false
        do {
            try await consumeStream(for: question, gotAnswer: &gotAnswer, sawProgress: &sawProgress)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            if sawProgress && !gotAnswer { throw StreamCutAfterProgress() }
            throw error
        }
        if !gotAnswer {
            // The stream closed with no "answer"/"error" event at all — a
            // proxy that buffers/drops SSE, most likely. Before any progress,
            // the caller's plain-request fallback is safe; after it, it is
            // a second paid run of the same question.
            if sawProgress { throw StreamCutAfterProgress() }
            throw APIClient.APIError(message: "Stream ended without an answer.")
        }
    }

    /// The stream failed after the server had started work on the question.
    struct StreamCutAfterProgress: Error {}

    private func consumeStream(for question: String, gotAnswer: inout Bool, sawProgress: inout Bool) async throws {
        for try await event in await client.stream(
            "/mobile/api/ask-cavnar/stream",
            body: StreamBody(question: question, conversation_id: conversationId,
                             new_conversation: wantsNewConversation)) {
            switch event.type {
            case "progress":
                sawProgress = true
                statusLabel = event.label
                if let raw = event.state, let mapped = CavnarOrbState(rawValue: raw) {
                    orbState = mapped
                }
            case "answer":
                gotAnswer = true
                adopt(conversationId: event.conversationId)
                appendAnswer(from: event.answer ?? "", truncated: event.truncated == true,
                            proposals: event.proposals ?? [], evidence: event.evidence)
            case "error":
                gotAnswer = true
                appendAnswer(from: event.error ?? "Something went wrong.", truncated: false,
                            proposals: [], evidence: nil)
            default:
                break
            }
        }
    }

    /// An answered question settles which chat we're in: a New chat now
    /// has an id, and the history list needs the new/updated row.
    private func adopt(conversationId id: Int?) {
        if let id { conversationId = id }
        wantsNewConversation = false
        Task { await refreshConversations() }
    }

    private func appendAnswer(from raw: String, truncated: Bool, proposals: [AskProposal],
                              evidence: AskEvidence?) {
        let cleaned = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let display = cleaned.isEmpty
            ? "I didn't get an answer back that time — mind asking again?"
            : cleaned
        // An empty strip is carried as nil so the view has one thing to check
        // rather than reaching into the struct to decide whether to draw.
        let ev = (evidence?.isEmpty == false) ? evidence : nil
        messages.append(ChatMessage(text: display, isUser: false, wasTruncated: truncated,
                                    proposals: proposals, evidence: ev))
    }
}


/// The assistant's opening: what needs the owner today, and the questions
/// worth asking given the state of the business right now.
struct AskOpening: Codable, Equatable {
    struct Item: Codable, Equatable, Identifiable {
        let severity: String?
        let title: String?
        let detail: String?
        let module: String?
        var id: String { (title ?? "") + (detail ?? "") }
    }
    let ok: Bool
    let headline: String?
    let briefing: [Item]?
    let suggestions: [String]?
    let restaurant: String?
    let sinceLabel: String?

    enum CodingKeys: String, CodingKey {
        case ok, headline, briefing, suggestions, restaurant
        case sinceLabel = "since_label"
    }
}
