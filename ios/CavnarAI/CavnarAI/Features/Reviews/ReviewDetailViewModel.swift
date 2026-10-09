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

    /// Why the reply guard wants this draft read before it posts, or nil.
    /// Starts from the review as it opened and follows every regenerate and
    /// save (their answers carry `needs_review` / `review_reason`), and a
    /// server refusal, so the banner and the confirm are about the text on
    /// screen, not the text the screen opened with.
    var flagReason: String?
    /// True while the "Post it anyway?" confirm for a flagged draft is up.
    var needsFlagConfirm = false
    /// The approve the flag confirm is holding was "Approve after all", so
    /// "Post it anyway" carries `approve_skipped` too.
    var flagConfirmApprovesSkipped = false
    static let defaultFlagReason = "states something Cavnar AI cannot confirm"
    /// drafter.FIX_REVIEW_REASON — a reply that names a change the owner
    /// marked done (memory round, 9/29/26). Held so the owner checks the
    /// wording matches what was done, not because it is unconfirmed.
    static let fixDoneReason = "mentions a change you marked done in Cavnar AI"

    /// The sentence after the reason in the post-anyway confirm.
    static func flagFollowUp(_ reason: String?) -> String {
        reason == fixDoneReason ? "Check it says what you actually changed."
                                : "Cavnar AI can\u{2019}t confirm that."
    }

    private let client: APIClient
    private var saveDraftTask: Task<Void, Never>?

    // MARK: - Which way the approve goes (readability round 10/8/26 #54)

    /// Whether the account's Google Business Profile is connected — nil
    /// until read. An approve posts only a Google review's reply, and only
    /// once Google is connected; everywhere else it approves.
    var googleConnected: Bool?

    /// The approve button's word: "Approve & post" only when this approve
    /// would go out (a Google review, Google connected), else "Approve".
    /// Unknown reads as "Approve" — never a claim it posts.
    var approveLabel: String {
        Self.approveLabel(platform: review.platform, googleConnected: googleConnected)
    }

    static func approveLabel(platform: String, googleConnected: Bool?) -> String {
        platform == "google" && googleConnected == true ? "Approve & post" : "Approve"
    }

    /// The account's connections, read once per ten minutes for every
    /// review opened in that time (a queue moves through many).
    private static var googleConnectedCache: (value: Bool, at: Date)?

    private struct AccountConnectionsProbe: Decodable {
        struct Status: Decodable { let connected: Bool? }
        struct Connections: Decodable {
            let googleBusiness: Status?
            enum CodingKeys: String, CodingKey { case googleBusiness = "google_business" }
        }
        let connections: Connections?
    }

    func loadGoogleConnection() async {
        guard review.platform == "google" else { return }
        if let connected = await Self.googleConnection(client: client) {
            googleConnected = connected
        }
    }

    /// Whether Google Business is connected, from the ten-minute cache or
    /// the account — nil when unknown. The inbox's approve confirm reads it
    /// too, to say where each reply goes.
    static func googleConnection(client: APIClient = .shared) async -> Bool? {
        if let cached = googleConnectedCache, Date().timeIntervalSince(cached.at) < 600 {
            return cached.value
        }
        guard let probe: AccountConnectionsProbe = try? await client.send("/mobile/api/account", hapticOnError: false),
              let connected = probe.connections?.googleBusiness?.connected else { return nil }
        googleConnectedCache = (connected, Date())
        return connected
    }

    init(review: Review, client: APIClient = .shared) {
        self.review = review
        self.currentStatus = review.responseStatus
        self.editedDraft = review.draftResponse ?? ""
        self.lastWrittenDraft = review.draftResponse ?? ""
        self.openedWithOwnEdits = review.draftEditedFlag == true
        self.client = client
        self.flagReason = review.draftIsFlagged
            ? (review.draftReviewReason ?? Self.defaultFlagReason) : nil
        // A Google post that failed before this screen opened (the row's
        // post_failed): the failure and its Retry are back on reopening —
        // they lived only as long as the screen that saw the approve.
        if review.isApproved && review.postFailed {
            self.postFailure = Self.savedFailureNote
            self.postFailedOnGoogle = true
        }
    }

    /// The web card's line under a failed post.
    static let savedFailureNote = "The reply is saved \u{2014} try posting it again."

    /// The review as it stands on this screen — its status and post state
    /// as this screen last left them — for the header's pill.
    var displayReview: Review {
        if markedElsewhere && currentStatus == "posted" { return review.withStatus("posted-elsewhere") }
        if currentStatus == review.responseStatus && postFailedOnGoogle == review.postFailed { return review }
        if currentStatus == "approved" && postFailedOnGoogle { return review.withStatus("approved-failed") }
        return review.withStatus(currentStatus)
    }

    /// The last post attempt reached Google and Google refused it (the
    /// row's post_failed) — as opposed to Google not being connected yet.
    var postFailedOnGoogle = false

    /// What the list should record for this review once it's left approved:
    /// "approved-failed" when Google refused the post.
    var listStatus: String {
        currentStatus == "approved" && postFailedOnGoogle ? "approved-failed" : currentStatus
    }

    /// The flag as a draft answer states it.
    private func applyFlag(_ response: DraftResponse) {
        flagReason = response.needsReview == true
            ? (response.reviewReason ?? Self.defaultFlagReason) : nil
    }

    /// `expectedDraft`: the reply on screen when Approve was tapped. The
    /// server posts only if that is still the stored reply (409
    /// `draft_changed` otherwise), so an approve that lands late — from the
    /// offline queue — can never publish an older draft than the one the
    /// owner read, even when the edit saved ahead of it was refused.
    ///
    /// `approveSkipped`: "Approve after all" on a reply shown as skipped —
    /// the only approve the server lets overturn a skip (re-audit 10/8/26).
    /// Never queued: a replay lands later than it was made.
    struct ApproveBody: Encodable {
        let confirmFlagged: Bool
        var expectedDraft: String? = nil
        var approveSkipped: Bool = false
        enum CodingKeys: String, CodingKey {
            case confirmFlagged = "confirm_flagged"
            case expectedDraft = "expected_draft"
            case approveSkipped = "approve_skipped"
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(confirmFlagged, forKey: .confirmFlagged)
            try c.encodeIfPresent(expectedDraft, forKey: .expectedDraft)
            if approveSkipped { try c.encode(true, forKey: .approveSkipped) }
        }
    }

    /// A 409 from approve for a draft flagged since this screen last knew.
    private struct FlagRefusal: Decodable {
        let needsReview: Bool?
        let reviewReason: String?
        enum CodingKeys: String, CodingKey {
            case needsReview = "needs_review"
            case reviewReason = "review_reason"
        }
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

    private struct NewTemplateBody: Encodable {
        let title: String
        let body: String
    }

    /// The reply on screen kept as a template — the web editor's "Save as
    /// template" (POST /mobile/api/templates, the route the app never
    /// called). Returns nil on success, else the sentence to show.
    func saveAsTemplate(title: String) async -> String? {
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let body = editedDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return "Give it a name you\u{2019}ll recognise." }
        guard !body.isEmpty else { return "Nothing to save \u{2014} write a reply first." }
        do {
            let response: APIClient.OKResponse = try await client.send(
                "/mobile/api/templates", method: .post, body: NewTemplateBody(title: name, body: body))
            guard response.ok else { return response.error ?? "Couldn\u{2019}t save that template." }
            Haptic.success()
            await loadTemplates()
            return nil
        } catch let error as APIClient.APIError {
            return error.message
        } catch {
            return "Couldn\u{2019}t save that template."
        }
    }

    /// DELETE /mobile/api/templates/<id>, asked first by the picker. The
    /// template leaves the list only once the server says it is gone — it
    /// used to vanish at once and come back on the next open when the
    /// delete had failed (re-audit 10/8/26 L3).
    func deleteTemplate(_ template: ResponseTemplate) async -> Bool {
        do {
            let response: APIClient.OKResponse = try await client.send(
                "/mobile/api/templates/\(template.id)", method: .delete, hapticOnError: false)
            guard response.ok else { return false }
            templates.removeAll { $0.id == template.id }
            return true
        } catch {
            return false
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

    /// `confirmFlagged`: the owner saw the flag's reason and chose to post
    /// anyway. A flagged draft without it stops at the confirm (the view's
    /// dialog calls back with true); the server refuses it too (M-1).
    ///
    /// `approveSkipped`: the skipped screen's "Approve after all" — sent
    /// as `approve_skipped`; every other approve leaves a skip standing.
    func approve(confirmFlagged: Bool = false, approveSkipped: Bool = false) async {
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
                // the first failure — never out live ahead of it. Unconfirmed
                // it carries no confirm, so the server refuses a draft that
                // turns out flagged rather than posting it unread.
                await queueApprove(confirmFlagged: confirmFlagged)
                return
            }
        }
        // Read first: the save above may have just flagged the edit.
        if flagReason != nil && !confirmFlagged {
            flagConfirmApprovesSkipped = approveSkipped
            needsFlagConfirm = true
            return
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
                "/mobile/api/reviews/\(review.id)/approve", method: .post,
                body: ApproveBody(confirmFlagged: confirmFlagged, expectedDraft: editedDraft,
                                  approveSkipped: approveSkipped)
            )
            Haptic.success()
            let status = response.posted ? "posted" : "approved"
            postFailure = response.shortfall
            postFailedOnGoogle = !response.posted && response.failedOnGoogle
            finalStatus = status
            currentStatus = status
            // A post Google refused keeps the owner on this screen, where
            // the retry is, instead of popping back to the list as a plain
            // success. A Yelp reply or one waiting on the Google connection
            // is a finished approve and moves on (re-audit 10/8/26 H2).
            didComplete = response.advancesQueue
        } catch let error as APIClient.APIError where error.isRetryable && !error.mayHaveReachedServer && approveSkipped {
            // "Approve after all" is never queued: a replay carries no
            // approve_skipped, so the server would leave the skip standing.
            errorMessage = "You\u{2019}re offline, so this reply wasn\u{2019}t approved. Try Approve after all again once you\u{2019}re back online."
        } catch let error as APIClient.APIError where error.isRetryable && !error.mayHaveReachedServer {
            // Never left the phone, so replaying it later is safe.
            await queueApprove(confirmFlagged: confirmFlagged)
        } catch let error as APIClient.APIError where error.isRetryable {
            // Timed out or dropped mid-request. The Google post runs inside
            // this request, so it may well have gone out; queueing it would
            // replay a second post and a second response.approved webhook
            // (CLIENT-6). The owner stays here and checks first.
            errorMessage = "We lost the connection before Google answered, so this reply may already be posted. "
                         + "Go back and reopen the review to see its status before approving again."
        } catch let error as APIClient.APIError {
            if !confirmFlagged, error.status == 409,
               let refusal = error.decodeBody(FlagRefusal.self), refusal.needsReview == true {
                // Flagged since this screen opened: show why, then ask.
                flagReason = refusal.reviewReason ?? Self.defaultFlagReason
                flagConfirmApprovesSkipped = approveSkipped
                needsFlagConfirm = true
                return
            }
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't approve — try again."
        }
    }

    private func queueApprove(confirmFlagged: Bool) async {
        await PendingWriteQueue.shared.enqueue(
            path: "/mobile/api/reviews/\(review.id)/approve",
            method: "POST",
            bodyJSON: try? JSONEncoder().encode(ApproveBody(confirmFlagged: confirmFlagged,
                                                            expectedDraft: editedDraft)),
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
        /// The reply guard's verdict on the text just written or saved.
        let needsReview: Bool?
        let reviewReason: String?
        enum CodingKeys: String, CodingKey {
            case ok, draft, error
            case needsReview = "needs_review"
            case reviewReason = "review_reason"
        }
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

    /// The draft as Cavnar AI last wrote it, and whether the words on screen
    /// are the owner's own — edited here, or saved edited before this screen
    /// opened (reviews.draft_edited). A regenerate asks before it replaces
    /// them (re-audit 10/8/26 M8): it used to swap them out with no way back.
    private var lastWrittenDraft: String
    private var openedWithOwnEdits: Bool
    var replyHasOwnEdits: Bool {
        let shown = editedDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !shown.isEmpty else { return false }
        return openedWithOwnEdits || shown != lastWrittenDraft.trimmingCharacters(in: .whitespacesAndNewlines)
    }

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
                lastWrittenDraft = draft
                openedWithOwnEdits = false
                applyFlag(response)
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
            applyFlag(response)
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
                markedElsewhere = false
                postFailure = nil
                postFailedOnGoogle = false
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

    /// Answered outside Cavnar AI — someone replied on Google by hand
    /// (10/2/26). Twin of the web's "Replied on Google": out of the queue,
    /// the urgent count and the reminders; nothing is posted. Undo is the
    /// ordinary undo(), which the server reverses for this mark.
    var markedElsewhere = false
    /// Posted because someone answered outside Cavnar AI, not because a
    /// draft of ours went out: Undo, never Retract.
    var isAnsweredElsewhere: Bool {
        currentStatus == "posted" && (markedElsewhere || review.repliedElsewhere)
    }

    @discardableResult
    func markRepliedElsewhere() async -> Bool {
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/reviews/\(review.id)/replied-elsewhere", method: .post
            )
            if response.ok {
                Haptic.light()
                markedElsewhere = true
                // Not "posted": that plays the reply-posted moment, and the
                // list reads this as answered elsewhere (Review.withStatus).
                finalStatus = "posted-elsewhere"
                currentStatus = "posted"
                didComplete = true
                return true
            }
            errorMessage = response.error ?? "Couldn't mark that one — try again."
            return false
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't mark that one — try again."
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
                postFailedOnGoogle = false
                currentStatus = "posted"
                finalStatus = "posted"
                return true
            }
            postFailure = response.shortfall ?? "Google isn't connected yet."
            postFailedOnGoogle = response.failedOnGoogle
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
