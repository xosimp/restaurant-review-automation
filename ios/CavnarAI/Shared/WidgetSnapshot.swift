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
    /// "+$525 vs budget" / "−$135 vs budget" — the night's net against its
    /// budget, for a login allowed the budget (dsr/access.py keeps it
    /// owner-only); nil otherwise, and on a snapshot written before it.
    var budgetLabel: String? = nil
    var budgetIsUp: Bool? = nil
    /// Last night's labor % and food cost % (parity audit #59) — the
    /// report's own KPI figures (value_text, so the widget cannot drift
    /// into a second format) with the number behind each for the gauge and
    /// the target it is held to. Nil when the night didn't measure one or
    /// this login's view of the report leaves it out (a manager's view has
    /// no food cost): the widget then says "—", never 0%.
    var laborLabel: String? = nil
    var laborPct: Double? = nil
    var laborTarget: Double? = nil
    var foodLabel: String? = nil
    var foodPct: Double? = nil
    var foodTarget: Double? = nil
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
    /// The "Last night" widget — the same snapshot, the night half only.
    static let lastNightWidgetKind = "CavnarLastNightWidget"
    /// Labor % and food cost % (#59) — the same snapshot's cost half.
    static let costsWidgetKind = "CavnarCostsWidget"
    /// Every widget drawn from this snapshot: the app reloads all of them
    /// whenever it writes or clears it.
    static let allWidgetKinds = [widgetKind, lastNightWidgetKind, costsWidgetKind]

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
    ///
    /// Every link names the location the figures are from (`?loc=`,
    /// re-audit 10/8/26 #6): a widget set to another of the group's
    /// locations opens that location's report, not the one the app is on.
    func link(now: Date = Date()) -> String {
        if waitingIsCurrent(now: now), waitingCount > 0 {
            return CavnarLink.located("cavnarai://command", restaurantId)
        }
        return nightLink
    }

    /// "Good day 82/100" — the verdict with its score when both exist;
    /// nil without a verdict.
    var verdictLine: String? {
        guard let v = nightVerdict, !v.isEmpty else { return nil }
        return nightScore.map { "\(v) \($0)/100" } ?? v
    }

    /// The "Last night" widget's link: that night's report when the
    /// snapshot knows the date, else the latest night.
    var nightLink: String {
        if let nightDate, !nightDate.isEmpty {
            return CavnarLink.located("cavnarai://nav/dsr/night/" + nightDate, restaurantId)
        }
        return CavnarLink.located("cavnarai://nav/dsr", restaurantId)
    }

    /// Last night's figures as one spoken sentence — what "How was last
    /// night?" answers without opening the app:
    /// "Last night, 9/24/26: $4,210 in net sales, +8% vs last Friday,
    /// +$525 vs budget. Good day, 82 out of 100."
    /// Nil while there is no current night with a measured net: the answer
    /// is then "no report yet", never an old night read as last night's.
    func lastNightSentence(now: Date = Date()) -> String? {
        guard nightIsCurrent(now: now), let net = netLabel, !net.isEmpty else { return nil }
        var parts = ["\(net) in net sales"]
        if let change = changeLabel {
            parts.append([change, changeBasis].compactMap { $0 }.joined(separator: " "))
        }
        if let budget = budgetLabel { parts.append(budget) }
        let lead = nightLabel.map { "Last night, \($0): " } ?? "Last night: "
        var sentence = lead + parts.joined(separator: ", ") + "."
        if let verdict = nightVerdict, !verdict.isEmpty {
            sentence += " " + verdict + (nightScore.map { ", \($0) out of 100" } ?? "") + "."
        }
        return sentence
    }

    /// "3 things waiting" / "1 thing waiting" / "Nothing waiting".
    var waitingLine: String {
        switch waitingCount {
        case ..<1: return "Nothing waiting"
        case 1: return "1 thing waiting"
        default: return "\(waitingCount) things waiting"
        }
    }

    /// "Labor 28.4% · Food 31.2%" — one line for the inline Lock Screen
    /// slot; "—" for a figure the night didn't measure.
    var costsLine: String {
        "Labor \(laborLabel ?? "—") \u{00B7} Food \(foodLabel ?? "—")"
    }

    // MARK: Storage

    private static var defaults: UserDefaults? { UserDefaults(suiteName: appGroup) }
    /// Every location's last snapshot, by restaurant id (#96): a widget set
    /// to a location shows that location's figures, as they were when the
    /// app last read them there — each half still going stale on its own.
    static let byLocationKey = "cavnar.widget.snapshots.byLocation.v1"

    /// The location the app is on now.
    static func load() -> WidgetSnapshot? {
        guard let data = defaults?.data(forKey: storageKey) else { return nil }
        return try? JSONDecoder().decode(WidgetSnapshot.self, from: data)
    }

    /// One location's snapshot; nil when the app has never read it there.
    static func load(restaurantId: Int) -> WidgetSnapshot? {
        if let active = load(), active.restaurantId == restaurantId { return active }
        return loadAll()[String(restaurantId)]
    }

    /// What a widget configured for `locationId` draws: that location's
    /// snapshot, or the active one's when it names none.
    static func forWidget(locationId: Int?) -> WidgetSnapshot? {
        guard let locationId else { return load() }
        return load(restaurantId: locationId)
    }

    private static func loadAll() -> [String: WidgetSnapshot] {
        guard let data = defaults?.data(forKey: byLocationKey),
              let all = try? JSONDecoder().decode([String: WidgetSnapshot].self, from: data) else { return [:] }
        return all
    }

    static func save(_ snapshot: WidgetSnapshot) {
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        defaults?.set(data, forKey: storageKey)
        guard let rid = snapshot.restaurantId, rid > 0 else { return }
        var all = loadAll()
        all[String(rid)] = snapshot
        if let blob = try? JSONEncoder().encode(all) { defaults?.set(blob, forKey: byLocationKey) }
    }

    /// Signed out: the widget must stop showing the restaurant's figures —
    /// every location's.
    static func clear() {
        defaults?.removeObject(forKey: storageKey)
        defaults?.removeObject(forKey: byLocationKey)
        WidgetLocations.clear()
    }
}

/// The locations a widget may be set to (#96): the app writes the list it
/// reads from /mobile/api/group-locations; the widget's location picker
/// (CavnarLocationQuery) reads it here — the extension never calls the API.
struct WidgetLocationOption: Codable, Hashable, Sendable {
    let id: Int
    let name: String
}

enum WidgetLocations {
    static let storageKey = "cavnar.widget.locations.v1"
    private static var defaults: UserDefaults? { UserDefaults(suiteName: WidgetSnapshot.appGroup) }

    static func load() -> [WidgetLocationOption] {
        guard let data = defaults?.data(forKey: storageKey),
              let list = try? JSONDecoder().decode([WidgetLocationOption].self, from: data) else { return [] }
        return list
    }

    static func save(_ list: [WidgetLocationOption]) {
        guard let data = try? JSONEncoder().encode(list) else { return }
        defaults?.set(data, forKey: storageKey)
    }

    static func clear() {
        defaults?.removeObject(forKey: storageKey)
    }
}

/// The location a widget or Live Activity link is about (re-audit 10/8/26
/// #6): `?loc=<restaurant id>` on a `cavnarai://` link, which SystemEntry
/// reads the way it reads a dashboard link's `loc=` — the app switches
/// there first when this login has it.
enum CavnarLink {
    static func located(_ raw: String, _ restaurantId: Int?) -> String {
        guard let restaurantId, restaurantId > 0 else { return raw }
        return raw + (raw.contains("?") ? "&" : "?") + "loc=\(restaurantId)"
    }
}
