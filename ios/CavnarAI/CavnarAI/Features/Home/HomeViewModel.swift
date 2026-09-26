import Foundation
import Observation

@Observable
@MainActor
final class HomeViewModel {
    var summary: HomeSummary?
    var isLoading = false
    var errorMessage: String?
    /// When the last successful fetch landed. Drives the foreground-refresh
    /// policy and the "showing data from earlier" notice — Home used to keep
    /// hours-old numbers on screen with nothing to indicate it (audit 4.2).
    private(set) var lastLoadedAt: Date?

    /// Home had no cache at all, so an offline launch showed a bare error
    /// screen instead of the numbers the owner opened the app to check
    /// (audit 6.4). Same pattern Labor already proved out, generalised.
    ///
    /// Keyed by user and restaurant (SessionScope.key), and replaced when the
    /// session changes, so one account's cached dashboard is never another's.
    @ObservationIgnored private var cache = CachedResource<HomeSummary>(key: SessionScope.key("home.summary"))
    /// The SessionScope generation `summary` belongs to.
    @ObservationIgnored private var loadedGeneration = SessionScope.generation

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// Drops everything from a previous sign-in or location before this
    /// session's first load paints anything.
    private func adoptCurrentSession() {
        guard loadedGeneration != SessionScope.generation else { return }
        loadedGeneration = SessionScope.generation
        summary = nil
        lastLoadedAt = nil
        errorMessage = nil
        cache = CachedResource<HomeSummary>(key: SessionScope.key("home.summary"))
    }

    /// Non-nil when what's on screen came from cache and is old enough that
    /// the owner should know before acting on it (audit 6.5).
    var stalenessNotice: String? {
        guard summary != nil, lastLoadedAt == nil, let cachedAt = cache.cachedAt else { return nil }
        let minutes = Int(Date().timeIntervalSince(cachedAt) / 60)
        if minutes < 5 { return nil }
        if minutes < 60 { return "Showing data from \(minutes)m ago" }
        return "Showing data from \(minutes / 60)h ago"
    }

    /// True while the action deck's one-tap publish is in flight.
    var isPublishingReplies = false

    /// Home's "Publish N replies" — approves every drafted reply server-side
    /// in one call (capped at 25 per tap, see mobile_api.py's
    /// mobile_approve_all_reviews), then reloads so the deck, the pulse
    /// strip and the value figure all catch up together. Returns nil on
    /// failure; APIClient has already played the error haptic by then.
    private struct PublishBody: Encodable { let limit: Int }

    /// `limit` is the number on the card's label; nil keeps the server's cap.
    func publishAllReplies(limit: Int? = nil) async -> BulkPublishResult? {
        isPublishingReplies = true
        defer { isPublishingReplies = false }
        do {
            let result: BulkPublishResult = try await client.send(
                "/mobile/api/reviews/approve-all", method: .post,
                body: limit.map { PublishBody(limit: $0) })
            await load()
            return result
        } catch {
            return nil
        }
    }

    private struct ProposeBody: Encodable {
        let action: String
        let args: [String: String]
    }

    /// "Publish N replies" asks first with the same confirm card the web
    /// and Ask render (/command/propose → approve_all_reviews, no model
    /// call): every reply that would post, in its own words, and how many
    /// the public-reply check holds back (parity audit #2). Nil when the
    /// route isn't there or can't build it — Home then confirms with the
    /// count, as before.
    func proposePublish() async -> AskProposal? {
        let r: CommandProposeResponse? = try? await client.send(
            "/mobile/api/command/propose", method: .post,
            body: ProposeBody(action: "approve_all_reviews", args: [:]), hapticOnError: false)
        guard let r, r.ok else { return nil }
        return r.proposal
    }

    private struct UndoBody: Encodable {
        let key: String
        let undo: Bool
    }

    /// "Restore hidden": the newest recommendation this login hid comes back
    /// (POST /home/dismiss {key, undo: true} — the web's undo), then Home
    /// re-reads. False when the server refused it.
    func restoreHidden(_ rec: HomeDismissedRec) async -> Bool {
        let r: APIClient.OKResponse? = try? await client.send(
            "/mobile/api/home/dismiss", method: .post, body: UndoBody(key: rec.key, undo: true))
        guard r?.ok == true else { return false }
        await load()
        return true
    }

    func load() async {
        adoptCurrentSession()
        let generation = SessionScope.generation
        // Warm start: paint cached numbers immediately rather than a loading
        // seal, and keep them on screen if the fetch fails.
        if summary == nil {
            summary = await cache.loadOffMain()
            if let cached = summary?.quickActions?.items { HomeQuickActionsStore.update(cached) }
        }
        isLoading = summary == nil
        errorMessage = nil
        defer { isLoading = false }
        do {
            let fetched: HomeSummary = try await client.send("/mobile/api/home")
            DebugFrameWatchdog.mark("home summary fetched")
            // Signed out or switched location while this was in flight: the
            // answer belongs to a session that no longer exists.
            guard generation == SessionScope.generation else { return }
            summary = fetched
            cache.save(fetched)
            // The command sheet's "One tap" row reads Home's own ranked list.
            HomeQuickActionsStore.update(fetched.quickActions?.items ?? [])
            lastLoadedAt = Date()
        } catch let error as APIClient.APIError {
            guard generation == SessionScope.generation else { return }
            // Only surface an error when there is genuinely nothing to show —
            // otherwise the cached dashboard stands and the staleness notice
            // explains itself. A refusal about the account itself (billing
            // paused, 402) is said whatever is on screen: old numbers must
            // not stand in for "your subscription is paused" (CLIENT-22).
            if summary == nil || error.kind == .billingInactive || error.status == 402 { errorMessage = error.message }
        } catch is APIClient.SessionExpiredError {
            // SessionStore's handler already forces logout — nothing more to do.
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch {
            if summary == nil { errorMessage = "Couldn't load your dashboard." }
        }
    }
}

/// POST /mobile/api/reviews/approve-all — how many drafted replies one tap
/// approved, how many of those are posting to Google right now, how many
/// failed, and how many drafts are still waiting (the call caps at 25).
struct BulkPublishResult: Decodable {
    let approved: Int
    let posted: Int
    let failed: Int
    let remaining: Int?
}
