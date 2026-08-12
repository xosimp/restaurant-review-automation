import Foundation
import Observation

@Observable
@MainActor
final class ReviewDetailViewModel {
    let review: Review
    /// review.responseStatus at the moment this screen opened. Mutable and
    /// separate from `review` (which stays a `let` — author/text/etc. never
    /// change) because undo/retract need the UI to flip back to the active
    /// Skip/Approve buttons in place, without dismissing the screen the way
    /// approve()/skip() do.
    var currentStatus: String
    var editedDraft: String
    var isSubmitting = false
    /// True only while the very-first auto-draft (ensureDraftIfNeeded) is in
    /// flight — distinct from isSubmitting, which also covers approve/skip/
    /// manual-regenerate/save, none of which should show the initial-load
    /// skeleton in the draft box.
    var isLoadingInitialDraft = false
    var errorMessage: String?
    /// Set to true once approve/skip succeeds — the detail view watches this
    /// to pop back to the list.
    var didComplete = false
    /// The status the review ended up at once didComplete fires — lets the
    /// list update that row in place instead of dropping it or needing a
    /// full reload to show the correct state on a later reopen.
    var finalStatus: String?

    var templates: [ResponseTemplate] = []

    private let client: APIClient
    private var saveDraftTask: Task<Void, Never>?

    init(review: Review, client: APIClient = .shared) {
        self.review = review
        self.currentStatus = review.responseStatus
        self.editedDraft = review.draftResponse ?? ""
        self.client = client
    }

    private struct TemplatesResponse: Decodable {
        let ok: Bool
        let templates: [ResponseTemplate]
    }

    func loadTemplates() async {
        do {
            // hapticOnError: false — same reasoning as Account's
            // loadBilling/loadSessions: a silent, non-fatal background load
            // with no visible error shouldn't buzz the same pattern as a
            // failed login.
            let response: TemplatesResponse = try await client.send(
                "/mobile/api/templates", hapticOnError: false
            )
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
            let response: ApproveResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/approve", method: .post
            )
            Haptic.success()
            finalStatus = (response.autoPosted == true) ? "posted" : "approved"
            currentStatus = finalStatus!
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
            // Lighter than approve()'s .success() — skip is a decisive dismissal,
            // not an accomplishment, so it gets the same weight as everywhere
            // else in the app that fires on a plain confirmed tap (Sign Out,
            // navigation) rather than the notification-style success buzz.
            Haptic.light()
            finalStatus = "skipped"
            currentStatus = "skipped"
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

    /// Server-side drafting normally runs as a batch job (see scheduler.py's
    /// daily fetch, which drafts every newly-ingested pending review) rather
    /// than on-demand — a review opened before that job has reached it has
    /// no draft yet. From the client's perspective there's no reason to ever
    /// show a blank box requiring a manual tap first, so a genuinely-missing
    /// draft is filled in automatically the moment the review is opened,
    /// using the same regenerate-draft endpoint the button already calls.
    func ensureDraftIfNeeded() async {
        guard editedDraft.isEmpty else { return }
        isLoadingInitialDraft = true
        await regenerateDraft()
        isLoadingInitialDraft = false
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

    private struct OkResponse: Decodable {
        let ok: Bool
        let error: String?
    }

    /// Undoes a skip, or an approval that never actually got auto-posted —
    /// neither has any external footprint, so this is just a status flip
    /// back to "drafted." Returns whether it succeeded so the view can
    /// update the list row without dismissing (unlike approve()/skip(),
    /// undo stays on this screen — the client is still looking at the same
    /// review, just with the active buttons back).
    @discardableResult
    func undo() async -> Bool {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/undo", method: .post
            )
            if response.ok {
                Haptic.light()
                currentStatus = "drafted"
                return true
            } else {
                errorMessage = response.error ?? "Couldn't undo — try again."
                return false
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't undo — try again."
            return false
        }
    }

    /// Retracts an auto-posted approval — actually deletes the live reply
    /// from Google first (server-side), only reverting to "drafted" once
    /// that really succeeds. A real API call with a real external effect,
    /// not a cosmetic undo, so failure here leaves the review exactly as
    /// posted rather than silently pretending it isn't.
    @discardableResult
    func retract() async -> Bool {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/retract", method: .post
            )
            if response.ok {
                Haptic.success()
                currentStatus = "drafted"
                return true
            } else {
                errorMessage = response.error ?? "Couldn't retract — try again."
                return false
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't retract — try again."
            return false
        }
    }
}
