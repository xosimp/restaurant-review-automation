import Foundation
import Observation

struct ChatMessage: Identifiable {
    let id = UUID()
    let text: String
    let isUser: Bool
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
    var messages: [ChatMessage] = []
    var question = ""
    var isLoading = false

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
    }

    func submit() async {
        guard canSubmit else { return }
        let asked = question
        // Captured from `messages` BEFORE appending the new question, so
        // this is exactly the prior back-and-forth — the backend appends
        // `question` itself as the final turn, matching ask_cavnar.py's
        // own `history` contract (prior turns only, not including the new
        // question).
        let history = messages.map { HistoryTurn(role: $0.isUser ? "user" : "assistant", content: $0.text) }
        messages.append(ChatMessage(text: asked, isUser: true))
        question = ""
        isLoading = true
        defer { isLoading = false }
        do {
            let response: AskResponse = try await client.send(
                "/mobile/api/ask-cavnar", method: .post, body: AskBody(question: asked, history: history)
            )
            let text = response.ok ? (response.answer ?? "") : (response.error ?? "Something went wrong.")
            messages.append(ChatMessage(text: text, isUser: false))
        } catch let error as APIClient.APIError {
            messages.append(ChatMessage(text: error.message, isUser: false))
        } catch {
            messages.append(ChatMessage(text: "Something went wrong. Try again.", isUser: false))
        }
    }
}
