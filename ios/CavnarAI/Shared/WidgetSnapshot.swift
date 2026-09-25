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
    /// store at a time, and the widget says which.
    var restaurantName: String?
    /// "9/24/26" — the business date of the report the net came from.
    var nightLabel: String?
    /// "$4,210", or nil when the night has no measured net (never "$0").
    var netLabel: String?
    /// "+8%" / "−3%", with the basis it was measured against.
    var changeLabel: String?
    var changeBasis: String?
    var changeIsUp: Bool?
    /// Open items in the action queue for this login (0 = nothing waiting).
    var waitingCount: Int
    /// Drafted replies waiting for approval — the quick action's subtitle.
    var pendingReplies: Int
    var updatedAt: Date

    static let empty = WidgetSnapshot(restaurantName: nil, nightLabel: nil, netLabel: nil, changeLabel: nil,
                                      changeBasis: nil, changeIsUp: nil, waitingCount: 0, pendingReplies: 0,
                                      updatedAt: .distantPast)

    /// Shared container both targets are entitled to (project.yml).
    static let appGroup = "group.ai.cavnar.CavnarAI"
    static let storageKey = "cavnar.widget.snapshot.v1"
    /// The widget's WidgetKit `kind` — the app reloads it by name.
    static let widgetKind = "CavnarWaitingWidget"

    /// A snapshot older than this is shown as "Open Cavnar to refresh"
    /// rather than as today's numbers.
    static let staleAfter: TimeInterval = 36 * 3600

    func isStale(now: Date = Date()) -> Bool {
        now.timeIntervalSince(updatedAt) > Self.staleAfter
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
