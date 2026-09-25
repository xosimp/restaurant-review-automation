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
    /// True only while approve() itself is running — narrower than
    /// isSubmitting (which also covers skip/regenerate/save/undo/retract)
    /// so the "Approve & Post" button's label only ever claims to be
    /// posting when it genuinely is. It used to read isSubmitting directly
    /// and said "Posting…" while a regenerate was running underneath it.
    var isApproving = false
    /// True while a draft is being written by Claude — both the explicit
    /// "Write a reply" button on an undrafted review and every manual
    /// Regenerate route through regenerateDraft(), which sets this for the
    /// duration of either. The draft box shows the composing-lines
    /// animation instead of the (stale) old draft while this is true.
    var isGeneratingDraft = false
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

    /// The answer, with `post_status` / `post_note` (ReviewPostOutcome):
    /// an approved reply that did not reach Google is a real, finished
    /// outcome — not "still working" — and the owner is told why and
    /// offered Retry posting.
    private typealias ApproveResponse = ReviewPostOutcome

    /// Non-nil when the last approve/retry approved the reply but could not
    /// publish it — the view shows the reason and a Retry posting button.
    var postFailure: String?
    /// True while retryPost() is running.
    var isRetryingPost = false

    func approve() async {
        // Flush any pending debounced edit first so what gets posted matches
        // what's on screen, rather than racing the 800ms save timer.
        //
        // The server posts the draft it has STORED. So the approve waits on
        // the save: a save that failed means the stored draft is still the
        // old one, and approving then published the pre-edit reply under
        // the restaurant's name (CLIENT-6).
        saveDraftTask?.cancel()
        if editedDraft != (review.draftResponse ?? "") {
            switch await saveDraft() {
            case .saved:
                break
            case .failed:
                return      // saveDraft already said why; nothing was posted
            case .queued:
                // The edit is waiting in the offline queue. The approve
                // goes in behind it — the queue drains in order and stops at
                // the first failure — never out live ahead of it.
                await queueApprove()
                return
            }
        }
        isSubmitting = true
        isApproving = true
        errorMessage = nil
        defer {
            isSubmitting = false
            isApproving = false
        }
        do {
            let response: ApproveResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/approve", method: .post
            )
            Haptic.success()
            let status = response.posted ? "posted" : "approved"
            postFailure = response.shortfall
            finalStatus = status
            currentStatus = status
            // A failed post keeps the owner on this screen, where the retry
            // is, instead of popping back to the list as a plain success.
            didComplete = (response.shortfall == nil)
        } catch let error as APIClient.APIError where error.isRetryable && !error.mayHaveReachedServer {
            // Never left the phone, so replaying it later is safe.
            await queueApprove()
        } catch let error as APIClient.APIError where error.isRetryable {
            // Timed out or dropped mid-request. The Google post runs inside
            // this request, so it may well have gone out; queueing it would
            // replay a second post and a second response.approved webhook
            // (CLIENT-6). The owner stays here and checks first.
            errorMessage = "We lost the connection before Google answered, so this reply may already be posted. "
                         + "Go back and reopen the review to see its status before approving again."
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't approve — try again."
        }
    }

    private func queueApprove() async {
        await PendingWriteQueue.shared.enqueue(
            path: "/mobile/api/reviews/\(review.id)/approve",
            method: "POST",
            bodyJSON: nil,
            label: "Approve response for \(review.author ?? "review")"
        )
        hasQueuedWrite = true
        // Locally optimistic but honestly labelled — the row shows a
        // "waiting to sync" state, not a claim that it posted.
        currentStatus = "pending-sync"
        didComplete = true
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

    /// Whether this review is waiting on a draft that nobody has asked for
    /// yet — the view offers a "Write a reply" button rather than spending
    /// a model call on its own.
    ///
    /// This used to auto-fire regenerateDraft() on open. Drafting is a
    /// Sonnet call billed against the restaurant's own AI budget, so simply
    /// browsing the inbox and tapping into N undrafted reviews spent N
    /// calls, silently, with no user intent behind any of them — and the
    /// web asks for an explicit click for exactly that reason.
    var needsDraft: Bool { editedDraft.isEmpty }

    /// Note: this route (like save-draft below) always answers HTTP 200 and
    /// signals failure only via the `ok`/`error` fields in the body — mirrors
    /// client_api.py's regenerate_draft(), which never sets an error status.
    func regenerateDraft() async {
        // Each draft is a paid model call. The button was only dimmed while
        // one ran, so a double tap paid for two (CLIENT-55).
        guard !isGeneratingDraft else { return }
        isSubmitting = true
        isGeneratingDraft = true
        errorMessage = nil
        defer {
            isSubmitting = false
            isGeneratingDraft = false
        }
        do {
            let response: DraftResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/regenerate-draft", method: .post
            )
            if response.ok, let draft = response.draft {
                editedDraft = draft
                announceDraft(draft)
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

    enum SaveOutcome { case saved, queued, failed }

    /// Posted when this screen writes or saves a draft, so the inbox's copy
    /// of the review carries it too (see ReviewsListViewModel).
    static let draftDidChange = Notification.Name("ai.cavnar.reviewDraftDidChange")

    private func announceDraft(_ draft: String) {
        NotificationCenter.default.post(name: Self.draftDidChange, object: nil,
                                        userInfo: ["id": review.id, "draft": draft])
    }

    @discardableResult
    func saveDraft() async -> SaveOutcome {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        let draft = editedDraft
        do {
            let response: DraftResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/save-draft", method: .post,
                body: SaveDraftBody(draft: draft)
            )
            if !response.ok {
                errorMessage = response.error ?? "Couldn't save your edit."
                return .failed
            }
            announceDraft(draft)
            return .saved
        } catch let error as APIClient.APIError where error.isRetryable {
            // Offline or a dropped connection: queue the edit instead of
            // discarding it. This is the exact loss the audit found — a
            // manager on one bar of signal edits a response, the debounced
            // autosave fails silently, and the work is gone (audit 6.1).
            let body = try? JSONEncoder().encode(SaveDraftBody(draft: editedDraft))
            await PendingWriteQueue.shared.enqueue(
                path: "/mobile/api/reviews/\(review.id)/save-draft",
                method: "POST",
                bodyJSON: body,
                label: "Save draft response"
            )
            hasQueuedWrite = true
            errorMessage = nil
            return .queued
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return .failed
        } catch {
            errorMessage = "Couldn't save — try again."
            return .failed
        }
    }

    /// True once an edit has been parked in the offline queue, so the UI can
    /// say "saved, will sync" rather than either lying about success or
    /// showing a bare failure.
    var hasQueuedWrite = false

    private typealias OkResponse = APIClient.OKResponse

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

    /// A reply the owner pasted onto Yelp/Facebook themselves — the web's
    /// "Mark as posted", for platforms Cavnar can't post to directly.
    @discardableResult
    func markPosted() async -> Bool {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/mark-posted", method: .post
            )
            if response.ok {
                Haptic.success()
                currentStatus = "posted"
                return true
            }
            errorMessage = response.error ?? "Couldn't mark that as posted."
            return false
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't mark that as posted."
            return false
        }
    }

    /// Soft-deletes the review (deleted_at server-side) — it leaves the
    /// inbox and every stat. Same route the web's Delete uses.
    @discardableResult
    func deleteReview() async -> Bool {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/delete", method: .post
            )
            if response.ok {
                Haptic.success()
                return true
            }
            errorMessage = response.error ?? "Couldn't delete that review."
            return false
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't delete that review."
            return false
        }
    }

    /// Re-attempts the Google post for a reply that was approved but never
    /// published. Same endpoint the web's "Retry posting" button uses.
    @discardableResult
    func retryPost() async -> Bool {
        // A second tap while the first is still posting would post twice
        // (CLIENT-55).
        guard !isRetryingPost else { return false }
        isRetryingPost = true
        defer { isRetryingPost = false }
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let response: ApproveResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/retry-post", method: .post
            )
            guard response.ok else {
                errorMessage = "Couldn't retry — try again."
                return false
            }
            if response.posted {
                Haptic.success()
                postFailure = nil
                currentStatus = "posted"
                finalStatus = "posted"
                return true
            }
            postFailure = response.shortfall ?? "Google isn't connected yet."
            return false
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't retry — try again."
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
