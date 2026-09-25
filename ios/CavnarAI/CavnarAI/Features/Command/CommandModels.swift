import Foundation

/// The mobile twins of the Command Center's three routes (Friction audit
/// #47/#48; built server-side by workstream N):
///   GET  /mobile/api/command/registry  — the commands this login may run here
///   GET  /mobile/api/command/search?q= — places, people, reviews, nights…
///   POST /mobile/api/command/propose   — Ask's confirm card, no model call
/// Same server, same commands and same confirm card as the web palette.

/// One command the server says this login may run. `kind` decides what a
/// tap does: "nav" opens `nav`, "action" asks the server for the confirm
/// card, "ask" hands the text to Ask Cavnar.
struct CommandEntry: Decodable, Identifiable, Hashable {
    let id: String
    let label: String
    let keywords: [String]
    let kind: String
    /// 0 read / navigate, 1 reversible write, 2 sends something outside.
    let tier: Int
    let nav: String?
    let action: String?
    let args: [String: AnyCodableValue]?

    enum CodingKeys: String, CodingKey { case id, label, keywords, kind, tier, nav, action, args }

    init(id: String, label: String, keywords: [String], kind: String, tier: Int = 0,
         nav: String? = nil, action: String? = nil, args: [String: AnyCodableValue]? = nil) {
        self.id = id
        self.label = label
        self.keywords = keywords
        self.kind = kind
        self.tier = tier
        self.nav = nav
        self.action = action
        self.args = args
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decode(String.self, forKey: .id)) ?? String((try? c.decode(Int.self, forKey: .id)) ?? 0)
        label = (try? c.decode(String.self, forKey: .label)) ?? ""
        keywords = (try? c.decodeIfPresent([String].self, forKey: .keywords)) ?? []
        kind = (try? c.decode(String.self, forKey: .kind)) ?? "nav"
        tier = (try? c.decodeIfPresent(Int.self, forKey: .tier)) ?? 0
        nav = try? c.decodeIfPresent(String.self, forKey: .nav)
        action = try? c.decodeIfPresent(String.self, forKey: .action)
        args = try? c.decodeIfPresent([String: AnyCodableValue].self, forKey: .args)
    }

    /// How well `query` matches: 3 label prefix, 2 a keyword prefix,
    /// 1 anywhere in the label, 0 not at all. Every word of the query must
    /// hit, so "send sch" finds "Send next week's schedule".
    func score(_ query: String) -> Int {
        let words = CommandMatch.words(query)
        guard !words.isEmpty else { return 0 }
        let label = self.label.lowercased()
        let labelWords = CommandMatch.words(label)
        let keys = keywords.map { $0.lowercased() }
        var total = 0
        for w in words {
            if labelWords.contains(where: { $0.hasPrefix(w) }) { total += label.hasPrefix(w) ? 3 : 2 }
            else if keys.contains(where: { $0.hasPrefix(w) }) { total += 2 }
            else if label.contains(w) { total += 1 }
            else { return 0 }
        }
        return total
    }
}

enum CommandMatch {
    static func words(_ text: String) -> [String] {
        text.lowercased()
            .split(whereSeparator: { !$0.isLetter && !$0.isNumber })
            .map(String.init)
    }

    /// Registry entries matching `query`, best first; ties keep server order.
    static func rank(_ entries: [CommandEntry], query: String, limit: Int = 6) -> [CommandEntry] {
        let scored = entries.enumerated().compactMap { i, e -> (Int, Int, CommandEntry)? in
            let s = e.score(query)
            return s > 0 ? (s, i, e) : nil
        }
        return scored.sorted { $0.0 != $1.0 ? $0.0 > $1.0 : $0.1 < $1.1 }.prefix(limit).map { $0.2 }
    }

    /// The places the sheet can always open, whether or not the registry
    /// route exists yet on this server — every module in the nav grammar.
    static let fallbackPlaces: [CommandEntry] = [
        CommandEntry(id: "nav:home", label: "Home", keywords: ["today", "brief"], kind: "nav", nav: "home"),
        CommandEntry(id: "nav:reviews", label: "Reviews", keywords: ["replies", "google", "stars"], kind: "nav", nav: "reviews"),
        CommandEntry(id: "nav:reviews-pending", label: "Replies to approve", keywords: ["approve", "drafted"],
                     kind: "nav", nav: "reviews?filter=pending"),
        CommandEntry(id: "nav:labor", label: "Labor", keywords: ["schedule", "staff", "shifts"], kind: "nav", nav: "labor"),
        CommandEntry(id: "nav:labor-requests", label: "Time off and shift requests", keywords: ["time off", "swap", "requests"],
                     kind: "nav", nav: "labor/requests"),
        CommandEntry(id: "nav:inventory", label: "Food Cost", keywords: ["inventory", "invoices", "orders", "count"],
                     kind: "nav", nav: "inventory"),
        CommandEntry(id: "nav:scan", label: "Scan an invoice", keywords: ["camera", "invoice", "receipt"],
                     kind: "nav", nav: "inventory/invoices?scan=camera"),
        CommandEntry(id: "nav:order", label: "Supplier order", keywords: ["order", "suppliers", "sysco"],
                     kind: "nav", nav: "inventory/order"),
        CommandEntry(id: "nav:count", label: "Count sheet", keywords: ["count", "stock"], kind: "nav", nav: "inventory/count"),
        CommandEntry(id: "nav:marketing", label: "Marketing", keywords: ["post", "instagram", "guests"], kind: "nav", nav: "marketing"),
        CommandEntry(id: "nav:intel", label: "Intel", keywords: ["competitors", "visibility"], kind: "nav", nav: "intel"),
        CommandEntry(id: "nav:dsr", label: "Last night's report", keywords: ["sales", "daily report", "dsr"], kind: "nav", nav: "dsr"),
        CommandEntry(id: "nav:account", label: "Account", keywords: ["settings", "team", "billing"], kind: "nav", nav: "account"),
    ]
}

struct CommandRegistryResponse: Decodable {
    let ok: Bool
    let commands: [CommandEntry]?
}

/// One search hit. `nav` is where it opens; `location` says which store it
/// belongs to, since a group owner searches every location they can see.
struct CommandSearchResult: Decodable, Identifiable, Hashable {
    struct Location: Decodable, Hashable {
        let id: Int
        let name: String
    }

    let type: String
    let itemId: String
    let title: String
    let subtitle: String?
    let nav: String?
    let location: Location?

    var id: String { type + ":" + itemId }

    enum CodingKeys: String, CodingKey { case type, id, title, subtitle, nav, location }

    init(type: String, itemId: String, title: String, subtitle: String? = nil, nav: String? = nil,
         location: Location? = nil) {
        self.type = type
        self.itemId = itemId
        self.title = title
        self.subtitle = subtitle
        self.nav = nav
        self.location = location
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = (try? c.decode(String.self, forKey: .type)) ?? "item"
        itemId = (try? c.decode(String.self, forKey: .id)) ?? String((try? c.decode(Int.self, forKey: .id)) ?? 0)
        title = (try? c.decode(String.self, forKey: .title)) ?? ""
        subtitle = try? c.decodeIfPresent(String.self, forKey: .subtitle)
        nav = try? c.decodeIfPresent(String.self, forKey: .nav)
        location = try? c.decodeIfPresent(Location.self, forKey: .location)
    }

    /// A person opens the person sheet in place; everything else navigates.
    var personKey: String? {
        if type == "person" { return itemId }
        guard let path = NavPath(nav), path.head == "person" else { return nil }
        return path.target
    }

    var systemImage: String {
        switch type {
        case "person": return "person.fill"
        case "review": return "star.bubble"
        case "schedule": return "calendar"
        case "issue": return "exclamationmark.bubble"
        case "supplier", "order": return "shippingbox"
        case "invoice": return "doc.text"
        case "dsr", "night": return "chart.bar.doc.horizontal"
        case "location": return "building.2"
        case "ask", "conversation": return "sparkles"
        default: return "arrow.up.right"
        }
    }
}

struct CommandSearchResponse: Decodable {
    let ok: Bool
    let results: [CommandSearchResult]?
}

struct CommandProposeResponse: Decodable {
    let ok: Bool
    let proposal: AskProposal?
    let error: String?
}

/// One open item from /mobile/api/actions (action_queue), with the `nav`
/// the server adds so a tap lands on the item, not the module's top.
struct CommandWaitingItem: Decodable, Identifiable, Hashable {
    let key: String
    let kind: String?
    let title: String
    let detail: String?
    let severity: String?
    let module: String?
    let nav: String?
    let count: Int?

    var id: String { key }

    /// time_off:12 / shift_request:8 — answered in place, like the Labor
    /// block and the notification buttons.
    var request: (kind: String, id: Int)? {
        let parts = key.split(separator: ":", maxSplits: 1)
        guard parts.count == 2, let id = Int(parts[1]) else { return nil }
        let kind = String(parts[0])
        guard kind == "time_off" || kind == "shift_request" else { return nil }
        return (kind, id)
    }

    /// Where a tap goes: the server's `nav`, else the item's module. An Ask
    /// proposal ("ask:<id>") opens as a proposal whatever head the server
    /// gave it — `action/<id>` is also a queued send's address, a different
    /// id space (F3-2).
    var destination: NavPath? {
        if let id = proposalId { return NavPath("proposal/\(id)") }
        return NavPath(nav) ?? NavPath(module)
    }

    /// The stored Ask proposal this row is, for key "ask:<id>".
    var proposalId: Int? {
        let parts = key.split(separator: ":", maxSplits: 1)
        guard parts.count == 2, parts[0] == "ask", let id = Int(parts[1]), id > 0 else { return nil }
        return id
    }
}

struct CommandWaitingResponse: Decodable {
    let ok: Bool
    let items: [CommandWaitingItem]?
}
