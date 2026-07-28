import Foundation
import Observation

struct ChatMessage: Identifiable {
    let id = UUID()
    let text: String
    let isUser: Bool
}

/// A single request/response Q&A, not a streaming chat — matches the web
/// panel's behavior (client_api.py's ask-cavnar route makes one Claude call
/// per question, no conversation history sent). Each question gets a fresh
/// context snapshot; there's no multi-turn memory to preserve here.
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

    private struct AskBody: Encodable {
        let question: String
    }

    private struct AskResponse: Decodable {
        let ok: Bool
        let answer: String?
        let error: String?
    }

    func submit() async {
        guard canSubmit else { return }
        let asked = question
        messages.append(ChatMessage(text: asked, isUser: true))
        question = ""
        isLoading = true
        defer { isLoading = false }
        do {
            let response: AskResponse = try await client.send(
                "/mobile/api/ask-cavnar", method: .post, body: AskBody(question: asked)
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
