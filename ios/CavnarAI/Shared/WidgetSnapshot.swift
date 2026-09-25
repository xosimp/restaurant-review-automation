import Foundation

/// What the Home Screen / Lock Screen widget shows: last night's net and its
/// measured change, and how many things are waiting on the owner (Friction
/// audit #47, U3-12 d).
///
/// Compiled into BOTH the app and the widget extension. The app writes it
/// (WidgetSnapshotService, after it has read the same routes Home reads);
/// the widget only reads it. The widget never calls the API and never holds
/// a token — an extension that could sign in would be a second session to
/// secure. Labels are formatted by the app with the app's own formatters
/// (DSRFormat), so the widget cannot drift into a second number format.
struct WidgetSnapshot: Codable, Equatable {
    /// The location the figures belong to — a group owner's phone shows one
    /// store at a time, and the widget says which. Set for a login with more
    /// than one location (nil otherwise: there is nothing to tell apart).
    var restaurantName: String?
    /// Which location wrote this — a snapshot from another location is
    /// never merged into this one's.
    var restaurantId: Int?
    /// "9/24/26" — the business date of the report the net came from.
    var nightLabel: String?
    /// The same night, ISO — the widget's link opens that night's report.
    var nightDate: String?
    /// "$4,210", or nil when the night has no measured net (never "$0").
    var netLabel: String?
    /// "+8%" / "−3%", with the basis it was measured against.
    var changeLabel: String?
    var changeBasis: String?
    var changeIsUp: Bool?
    /// Last night's verdict from the stored scorecard — "Good day", its
    /// tone ("good" / "warn" / "bad") and the 0–100 score (density #50):
    /// a dot beside LAST NIGHT, and the medium widget's verdict line. Nil
    /// when the night has no scorecard (a manager's login, an older
    /// server), and on a snapshot written before these existed.
    var nightVerdict: String? = nil
    var nightTone: String? = nil
    var nightScore: Int? = nil
    /// Open items in the action queue for this login (0 = nothing waiting).
    var waitingCount: Int
    /// Drafted replies waiting for approval — the quick action's subtitle.
    var pendingReplies: Int
    var updatedAt: Date
    /// When each half was last READ — each has its own: a failed read of one
    /// keeps the other's last good value rather than zeroing it (F3-8).
    /// Nil on a snapshot written before these existed (updatedAt stands in).
    var waitingUpdatedAt: Date?
    var nightUpdatedAt: Date?

    static let empty = WidgetSnapshot(restaurantName: nil, restaurantId: nil, nightLabel: nil, nightDate: nil,
                                      netLabel: nil, changeLabel: nil, changeBasis: nil, changeIsUp: nil,
                                      waitingCount: 0, pendingReplies: 0, updatedAt: .distantPast)

    /// Shared container both targets are entitled to (project.yml).
    static let appGroup = "group.ai.cavnar.CavnarAI"
    static let storageKey = "cavnar.widget.snapshot.v1"
    /// The widget's WidgetKit `kind` — the app reloads it by name.
    static let widgetKind = "CavnarWaitingWidget"

    /// Last night's figures stay good for a day and a half — the next
    /// night's report replaces them.
    static let staleAfter: TimeInterval = 36 * 3600
    /// "3 things waiting" is a count of right now: past a few hours it is
    /// "Open Cavnar to refresh", never a morning's count read as the
    /// evening's (F3-8).
    static let waitingStaleAfter: TimeInterval = 6 * 3600

    func waitingIsCurrent(now: Date = Date()) -> Bool {
        now.timeIntervalSince(waitingUpdatedAt ?? updatedAt) <= Self.waitingStaleAfter
    }

    func nightIsCurrent(now: Date = Date()) -> Bool {
        now.timeIntervalSince(nightUpdatedAt ?? updatedAt) <= Self.staleAfter
    }

    /// Nothing in it is current any more.
    func isStale(now: Date = Date()) -> Bool {
        !waitingIsCurrent(now: now) && !nightIsCurrent(now: now)
    }

    /// The widget's link: that night's report when the snapshot knows the
    /// date (bare "dsr" is the latest night), the command sheet while
    /// something is waiting.
    func link(now: Date = Date()) -> String {
        if waitingIsCurrent(now: now), waitingCount > 0 { return "cavnarai://command" }
        if let nightDate, !nightDate.isEmpty { return "cavnarai://nav/dsr/night/" + nightDate }
        return "cavnarai://nav/dsr"
    }

    /// "Good day 82/100" — the verdict with its score when both exist;
    /// nil without a verdict.
    var verdictLine: String? {
        guard let v = nightVerdict, !v.isEmpty else { return nil }
        return nightScore.map { "\(v) \($0)/100" } ?? v
    }

    /// "3 things waiting" / "1 thing waiting" / "Nothing waiting".
    var waitingLine: String {
        switch waitingCount {
        case ..<1: return "Nothing waiting"
        case 1: return "1 thing waiting"
        default: return "\(waitingCount) things waiting"
        }
    }

    // MARK: Storage

    private static var defaults: UserDefaults? { UserDefaults(suiteName: appGroup) }

    static func load() -> WidgetSnapshot? {
        guard let data = defaults?.data(forKey: storageKey) else { return nil }
        return try? JSONDecoder().decode(WidgetSnapshot.self, from: data)
    }

    static func save(_ snapshot: WidgetSnapshot) {
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        defaults?.set(data, forKey: storageKey)
    }

    /// Signed out: the widget must stop showing the restaurant's figures.
    static func clear() {
        defaults?.removeObject(forKey: storageKey)
    }
}
