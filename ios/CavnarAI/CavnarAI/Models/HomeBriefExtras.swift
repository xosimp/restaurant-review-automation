import Foundation

// The rest of web Home's brief on GET /mobile/api/home (parity audit #1):
// quick actions, what changed since the last visit, the signal charts, the
// undo for a hidden recommendation and the wins. Every type decodes
// leniently — an odd value is empty or skipped, never a Home that fails to
// decode — because the whole summary is also the offline cache.

/// A list read element by element: an entry that doesn't decode is skipped,
/// and a value that isn't a list is empty.
struct HomeLenientList<Element: Codable & Hashable>: Codable, Hashable {
    let items: [Element]

    init(_ items: [Element]) { self.items = items }

    init(from decoder: Decoder) throws {
        guard var list = try? decoder.unkeyedContainer() else { items = []; return }
        var out: [Element] = []
        while !list.isAtEnd {
            let before = list.currentIndex
            if let e = try? list.decode(Element.self) {
                out.append(e)
            } else {
                _ = try? list.decode(JSONValue.self)
            }
            if list.currentIndex == before { break }
        }
        items = out
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(items)
    }
}

/// One of the header's one-tap actions (home_brief `quick_actions`):
/// `kind` is "publish_replies", "open_module", "ask" or "alerts"; `nav` is
/// where a tap lands (home_brief.QUICK_NAV).
struct HomeQuickAction: Codable, Hashable, Identifiable {
    let key: String
    let label: String
    let kind: String
    let module: String?
    let count: Int?
    let nav: String?

    var id: String { key }

    init(key: String, label: String, kind: String, module: String? = nil, count: Int? = nil, nav: String? = nil) {
        self.key = key; self.label = label; self.kind = kind
        self.module = module; self.count = count; self.nav = nav
    }

    enum CodingKeys: String, CodingKey { case key, label, kind, module, count, nav }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        label = try c.decode(String.self, forKey: .label)
        kind = ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil) ?? "open_module"
        module = (try? c.decodeIfPresent(String.self, forKey: .module)) ?? nil
        count = (try? c.decodeIfPresent(Int.self, forKey: .count)) ?? nil
        let n = (try? c.decodeIfPresent(String.self, forKey: .nav)) ?? nil
        nav = (n?.isEmpty ?? true) ? nil : n
    }

    /// Where a tap lands: the server's nav, else the module's top.
    var destination: String? { nav ?? module }

    /// The chip's words: the label, with the count after it when it adds
    /// something (a publish label already says its number).
    var chipLabel: String {
        guard let n = count, n > 0, kind != "publish_replies" else { return label }
        return "\(label) \(n)"
    }

    /// The row Home draws (web `hbQuickUnsaid` + `renderQuick`): never an
    /// action a Needs-attention item already carries (its kind or its
    /// destination), and never Ask or All alerts — the FAB and the bell are
    /// always on screen. The page says each action once.
    static func unsaid(_ actions: [HomeQuickAction], attention: [NeedsAttentionItem]) -> [HomeQuickAction] {
        var kinds = Set<String>(), navs = Set<String>()
        for a in attention {
            // "open_module" is generic: it only repeats a quick action
            // that opens the same place, which the nav match catches.
            if let k = a.action, k != "open_module" { kinds.insert(k) }
            if let n = a.nav { navs.insert(n) }
        }
        return actions.filter { q in
            q.kind != "ask" && q.kind != "alerts" && !kinds.contains(q.kind)
                && !(q.nav.map { navs.contains($0) } ?? false)
        }
    }
}

/// The quick actions Home last loaded, for the command sheet's "One tap"
/// row (the web palette reads the same list, `window._hbQuick`). Held per
/// session generation, so another account's or location's never shows.
@MainActor
enum HomeQuickActionsStore {
    private static var generation = -1
    private static var stored: [HomeQuickAction] = []

    static func update(_ actions: [HomeQuickAction]) {
        generation = SessionScope.generation
        stored = actions
    }

    /// Empty until Home has loaded for this session.
    static var current: [HomeQuickAction] {
        generation == SessionScope.generation ? stored : []
    }
}

/// `changes`: what changed since this login last looked — `since_label` is
/// "since yesterday" or "since your last sign-in".
struct HomeChanges: Codable, Hashable {
    struct Item: Codable, Hashable {
        let text: String
        let tone: String?
        let module: String?
    }

    let sinceLabel: String?
    let items: [Item]

    enum CodingKeys: String, CodingKey {
        case items
        case sinceLabel = "since_label"
    }

    init(sinceLabel: String?, items: [Item]) {
        self.sinceLabel = sinceLabel; self.items = items
    }

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        sinceLabel = (try? c?.decodeIfPresent(String.self, forKey: .sinceLabel)) ?? nil
        items = ((try? c?.decodeIfPresent(HomeLenientList<Item>.self, forKey: .items)) ?? nil)?.items ?? []
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(sinceLabel, forKey: .sinceLabel)
        try c.encode(items, forKey: .items)
    }

    /// The header line web Home draws (`renderTop`): "Since your last
    /// visit: 2 new reviews · Labor 31% yesterday · 3 items running low" —
    /// the first three changes, each without its " — why" tail. Nil when
    /// nothing changed (the hero's overnight line already says so).
    var line: String? {
        guard !items.isEmpty else { return nil }
        let raw = (sinceLabel ?? "since yesterday")
            .replacingOccurrences(of: "since your last sign-in", with: "since your last visit")
        let label = raw.prefix(1).uppercased() + raw.dropFirst()
        let parts = items.prefix(3).map { Self.head($0.text) }
        return label + ": " + parts.joined(separator: " \u{00B7} ")
    }

    private static func head(_ text: String) -> String {
        guard let r = text.range(of: " \u{2014} ") else { return text }
        return String(text[..<r.lowerBound])
    }
}

/// `charts`: the trend behind each pulse chip, drawn in Results (web
/// `renderSignals`). Every series may be empty — a module with no live data
/// sends none, and draws no tile.
struct HomeCharts: Codable, Hashable {
    struct RatingWeek: Codable, Hashable {
        let label: String
        let avg: Double
        let total: Int
    }
    struct LaborDay: Codable, Hashable {
        let day: String
        let pct: Double
    }
    struct WasteItem: Codable, Hashable {
        let item: String
        let cost: Double
    }

    let rating: [RatingWeek]
    let laborDays: [LaborDay]
    let laborTarget: Double?
    let waste: [WasteItem]

    enum CodingKeys: String, CodingKey {
        case rating, waste
        case laborDays = "labor_days"
        case laborTarget = "labor_target"
    }

    init(rating: [RatingWeek], laborDays: [LaborDay], laborTarget: Double?, waste: [WasteItem]) {
        self.rating = rating; self.laborDays = laborDays; self.laborTarget = laborTarget; self.waste = waste
    }

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        rating = ((try? c?.decodeIfPresent(HomeLenientList<RatingWeek>.self, forKey: .rating)) ?? nil)?.items ?? []
        laborDays = ((try? c?.decodeIfPresent(HomeLenientList<LaborDay>.self, forKey: .laborDays)) ?? nil)?.items ?? []
        laborTarget = (try? c?.decodeIfPresent(Double.self, forKey: .laborTarget)) ?? nil
        waste = ((try? c?.decodeIfPresent(HomeLenientList<WasteItem>.self, forKey: .waste)) ?? nil)?.items ?? []
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(rating, forKey: .rating)
        try c.encode(laborDays, forKey: .laborDays)
        try c.encodeIfPresent(laborTarget, forKey: .laborTarget)
        try c.encode(waste, forKey: .waste)
    }

    /// A trend needs two weeks; a one-week "trend" is a dot.
    var hasRatingTrend: Bool { rating.count > 1 }
    var isEmpty: Bool { !hasRatingTrend && laborDays.isEmpty && waste.isEmpty }
}

/// A recommendation this login hid — `until` when it may come back.
struct HomeDismissedRec: Codable, Hashable, Identifiable {
    let key: String
    let title: String
    let until: String?
    var id: String { key }
}

/// One thing that went right (home_brief `wins`).
struct HomeWin: Codable, Hashable {
    let title: String
    let detail: String?
    let module: String?
}
