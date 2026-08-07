import Foundation
import Observation

@Observable
@MainActor
final class ReviewDetailViewModel {
    let review: Review
    var editedDraft: String
    var isSubmitting = false
    var errorMessage: String?
    /// Set to true once approve/skip succeeds — the detail view watches this
    /// to pop back to the list.
    var didComplete = false

    var templates: [ResponseTemplate] = []

    private let client: APIClient
    private var saveDraftTask: Task<Void, Never>?

    init(review: Review, client: APIClient = .shared) {
        self.review = review
        self.editedDraft = review.draftResponse ?? ""
        self.client = client
    }

    private struct TemplatesResponse: Decodable {
        let ok: Bool
        let templates: [ResponseTemplate]
    }

    func loadTemplates() async {
        do {
            let response: TemplatesResponse = try await client.send("/mobile/api/templates")
            templates = response.templates
        } catch {
            // Non-fatal — the draft editor still works without saved templates.
        }
    }

    func applyTemplate(_ template: ResponseTemplate) {
        editedDraft = template.body
        scheduleDraftSave()
        Task {
            let _: APIClient.EmptyResponse? = try? await client.send(
                "/mobile/api/templates/\(template.id)/use", method: .post
            )
        }
    }

    /// Debounced so typing doesn't fire a save request per keystroke — waits
    /// for a short pause before actually calling saveDraft().
    func scheduleDraftSave() {
        saveDraftTask?.cancel()
        saveDraftTask = Task {
            try? await Task.sleep(for: .milliseconds(800))
            guard !Task.isCancelled else { return }
            await saveDraft()
        }
    }

    private struct ApproveResponse: Decodable {
        let ok: Bool
        let autoPosted: Bool?

        enum CodingKeys: String, CodingKey {
            case ok
            case autoPosted = "auto_posted"
        }
    }

    func approve() async {
        // Flush any pending debounced edit first so what gets posted matches
        // what's on screen, rather than racing the 800ms save timer.
        saveDraftTask?.cancel()
        if editedDraft != (review.draftResponse ?? "") {
            await saveDraft()
        }
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let _: ApproveResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/approve", method: .post
            )
            Haptic.success()
            didComplete = true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't approve — try again."
        }
    }

    func skip() async {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let _: APIClient.EmptyResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/skip", method: .post
            )
            didComplete = true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't skip — try again."
        }
    }

    private struct DraftResponse: Decodable {
        let ok: Bool
        let draft: String?
        let error: String?
    }

    /// Note: this route (like save-draft below) always answers HTTP 200 and
    /// signals failure only via the `ok`/`error` fields in the body — mirrors
    /// client_api.py's regenerate_draft(), which never sets an error status.
    func regenerateDraft() async {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let response: DraftResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/regenerate-draft", method: .post
            )
            if response.ok, let draft = response.draft {
                editedDraft = draft
            } else {
                errorMessage = response.error ?? "Couldn't regenerate the draft."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't regenerate — try again."
        }
    }

    private struct SaveDraftBody: Encodable {
        let draft: String
    }

    func saveDraft() async {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let response: DraftResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/save-draft", method: .post,
                body: SaveDraftBody(draft: editedDraft)
            )
            if !response.ok {
                errorMessage = response.error ?? "Couldn't save your edit."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't save — try again."
        }
    }
}
