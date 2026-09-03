import Foundation
import Observation

struct ChatMessage: Identifiable {
    let id = UUID()
    let text: String
    let isUser: Bool
    /// True when the model hit max_tokens — the answer stops mid-thought and
    /// must not be presented as a finished one (audit 5.1).
    var wasTruncated: Bool = false
}

/// Real multi-turn chat now, not one-off Q&A — the backend has no memory of
/// its own between calls (each is a stateless Claude request), so this view
/// model's own `messages` array IS the conversation's memory, sent back as
/// `history` on every subsequent question. Without this, a short follow-up
/// like "yes" arrived at the backend as a completely isolated question with
/// no idea what it was replying to — real bug, reported live.
@Observable
@MainActor
final class AskCavnarViewModel {
    /// Mirrors ask_cavnar.py's `_MAX_HISTORY_MESSAGES`. Anything older is
    /// discarded server-side anyway; sending it only cost upload time on the
    /// weak connections this app runs on (audit 5.4). Also caps what is held
    /// in memory for the life of the sheet (audit 3.4).
    static let maxHistoryMessages = 12
    /// Mirrors ask_cavnar.py's `question.strip()[:500]`. Enforced here so the
    /// user sees the limit rather than having the tail of their question
    /// silently cut server-side (audit 5.2).
    static let maxQuestionLength = 500

    var messages: [ChatMessage] = [] {
        didSet {
            if messages.count > Self.maxHistoryMessages * 2 {
                messages.removeFirst(messages.count - Self.maxHistoryMessages * 2)
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
    /// Transient failure notice shown above the input bar. Deliberately not a
    /// ChatMessage: an error appended as an assistant turn ends up replayed to
    /// Claude in `history` as something it supposedly said (audit 2.5).
    var errorBanner: String?

    var remainingCharacters: Int { Self.maxQuestionLength - question.count }

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    var canSubmit: Bool {
        !question.trimmingCharacters(in: .whitespaces).isEmpty && !isLoading
    }

    private struct HistoryTurn: Encodable {
        let role: String
        let content: String
    }

    private struct AskBody: Encodable {
        let question: String
        let history: [HistoryTurn]
    }

    private struct AskResponse: Decodable {
        let ok: Bool
        let answer: String?
        let error: String?
        let truncated: Bool?
    }

    func submit() async {
        guard canSubmit else { return }
        let asked = question
        errorBanner = nil
        // Captured from `messages` BEFORE appending the new question, so
        // this is exactly the prior back-and-forth — the backend appends
        // `question` itself as the final turn, matching ask_cavnar.py's
        // own `history` contract (prior turns only, not including the new
        // question). Trimmed to the server's own caps.
        let history = messages
            .suffix(Self.maxHistoryMessages)
            .map { HistoryTurn(role: $0.isUser ? "user" : "assistant", content: String($0.text.prefix(800))) }
        messages.append(ChatMessage(text: asked, isUser: true))
        question = ""
        isLoading = true
        defer { isLoading = false }
        do {
            let response: AskResponse = try await client.send(
                "/mobile/api/ask-cavnar", method: .post, body: AskBody(question: asked, history: history)
            )
            let raw = response.ok ? (response.answer ?? "") : (response.error ?? "Something went wrong.")
            let cleaned = cavnarPlainText(raw)
            let display = cleaned.isEmpty
                ? "I didn't get an answer back that time — mind asking again?"
                : cleaned
            messages.append(ChatMessage(
                text: display, isUser: false, wasTruncated: response.truncated == true
            ))
        } catch is CancellationError {
            // The sheet went away mid-request — roll the turn back silently.
            if messages.last?.isUser == true { messages.removeLast() }
            question = asked
        } catch {
            // Roll the turn back rather than leaving a dead end: the question
            // returns to the input box ready to resend (it used to be cleared
            // and lost, making "try again" harder to follow than it sounds),
            // and the failure never enters `history` (audit 2.5).
            if messages.last?.isUser == true { messages.removeLast() }
            question = asked
            errorBanner = (error as? APIClient.APIError)?.message
                ?? "Couldn't reach Cavnar AI — check your connection and try again."
        }
    }
}
