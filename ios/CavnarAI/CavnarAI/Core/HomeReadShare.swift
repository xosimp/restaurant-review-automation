import Foundation

/// What Home just read, lent to the widget refresh (the widget-read dedupe).
///
/// Home reads /mobile/api/actions and /mobile/api/dsr?limit=1 as it comes
/// up, and the widget refresh used to read the same two routes at the same
/// moment — the app becoming active starts both. Home's reads now land here,
/// and a refresh within `freshFor` of one uses it instead of reading again;
/// a refresh that starts while Home's read is still in flight waits for it
/// (up to `waitFor`).
///
/// Only Home's reads stand in for the widget's, never the other way round:
/// Home's queue read presents the queue (it IS the owner looking), the
/// widget's is a `peek`. And only for the location both were read for.
@MainActor
final class HomeReadShare {
    static let shared = HomeReadShare()

    struct Actions: Equatable {
        let count: Int
        let replies: Int
        let restaurantId: Int
        let at: Date
    }

    struct Night {
        /// The latest night's list row; nil when this login has no report
        /// (403, or no night yet) — a real answer, which clears the figures.
        let latest: DSRSummary?
        let restaurantId: Int
        let at: Date
    }

    /// A read older than this is the widget's to make again.
    nonisolated static let freshFor: TimeInterval = 90
    /// How long a refresh waits on a Home read already in flight.
    nonisolated static let waitFor: TimeInterval = 8

    private var actions: Actions?
    private var night: Night?
    private var actionsStarted: Date?
    private var nightStarted: Date?

    init() {}

    // MARK: Home's side

    func beginActions() { actionsStarted = Date() }

    /// Home's queue read: each item's key and count (nil when it failed).
    func finishActions(_ items: [(key: String, count: Int?)]?, restaurantId: Int = SessionScope.activeRestaurantId) {
        actionsStarted = nil
        guard let items else { return }
        let part = WidgetSnapshotService.waiting(fromHome: items)
        actions = Actions(count: part.count, replies: part.replies, restaurantId: restaurantId, at: Date())
    }

    func beginNight() { nightStarted = Date() }

    /// Home's night read: the latest list row, `.some(nil)` for "no report
    /// for this login", nil when the read failed.
    func finishNight(_ latest: DSRSummary??, restaurantId: Int = SessionScope.activeRestaurantId) {
        nightStarted = nil
        guard let latest else { return }
        night = Night(latest: latest, restaurantId: restaurantId, at: Date())
    }

    /// A location switch or sign-out: nothing read before it is lent.
    func reset() {
        actions = nil
        night = nil
        actionsStarted = nil
        nightStarted = nil
    }

    // MARK: The widget's side

    func recentActions(restaurantId: Int, now: Date = Date()) async -> Actions? {
        await waitWhile { self.actionsStarted }
        guard let a = actions, a.restaurantId == restaurantId,
              Date().timeIntervalSince(a.at) <= Self.freshFor else { return nil }
        return a
    }

    func recentNight(restaurantId: Int) async -> Night? {
        await waitWhile { self.nightStarted }
        guard let n = night, n.restaurantId == restaurantId,
              Date().timeIntervalSince(n.at) <= Self.freshFor else { return nil }
        return n
    }

    /// Waits while a read that began less than `waitFor` ago is in flight.
    private func waitWhile(_ started: () -> Date?) async {
        while let s = started(), Date().timeIntervalSince(s) < Self.waitFor {
            do { try await Task.sleep(for: .milliseconds(200)) } catch { return }
        }
    }
}
