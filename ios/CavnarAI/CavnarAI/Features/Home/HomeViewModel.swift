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
    private let cache = CachedResource<HomeSummary>(key: "home.summary")

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
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

    func load() async {
        // Warm start: paint cached numbers immediately rather than a loading
        // seal, and keep them on screen if the fetch fails.
        if summary == nil { summary = cache.load() }
        isLoading = summary == nil
        errorMessage = nil
        defer { isLoading = false }
        do {
            let fetched: HomeSummary = try await client.send("/mobile/api/home")
            summary = fetched
            cache.save(fetched)
            lastLoadedAt = Date()
        } catch let error as APIClient.APIError {
            // Only surface an error when there is genuinely nothing to show —
            // otherwise the cached dashboard stands and the staleness notice
            // explains itself.
            if summary == nil { errorMessage = error.message }
        } catch is APIClient.SessionExpiredError {
            // SessionStore's handler already forces logout — nothing more to do.
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch {
            if summary == nil { errorMessage = "Couldn't load your dashboard." }
        }
    }
}
