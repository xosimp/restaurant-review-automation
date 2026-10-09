import Foundation

/// GET /mobile/api/home/brief/group (home_brief.build_group_brief) — every
/// location in the owner's own group, side by side. Only what the phone's
/// switcher draws is decoded, and every field leniently: an odd value is
/// nil, never a switcher that fails to open. Numbers are never averaged
/// across locations; each stays attached to its own.
struct LocationGroupBrief: Decodable {
    struct Night: Decodable {
        let net: Double?
        let label: String?
        /// The group total's reach: how many locations measured that night.
        let locations: Int?
        let of: Int?
        let vsBudget: Double?
        /// One location's night (dsr.access.summary): its date and state —
        /// "final" / "provisional" / "failed" with no net is "not measured".
        var businessDate: String? = nil
        var status: String? = nil

        enum CodingKeys: String, CodingKey {
            case net, label, locations, of, status
            case vsBudget = "vs_budget"
            case businessDate = "business_date"
        }

        init(from decoder: Decoder) throws {
            let c = try? decoder.container(keyedBy: CodingKeys.self)
            net = (try? c?.decodeIfPresent(Double.self, forKey: .net)) ?? nil
            label = (try? c?.decodeIfPresent(String.self, forKey: .label)) ?? nil
            locations = (try? c?.decodeIfPresent(Int.self, forKey: .locations)) ?? nil
            of = (try? c?.decodeIfPresent(Int.self, forKey: .of)) ?? nil
            vsBudget = (try? c?.decodeIfPresent(Double.self, forKey: .vsBudget)) ?? nil
            businessDate = (try? c?.decodeIfPresent(String.self, forKey: .businessDate)) ?? nil
            status = (try? c?.decodeIfPresent(String.self, forKey: .status)) ?? nil
        }
    }

    /// A location's reviews over 30 days (home_brief._location_record).
    struct Reviews: Decodable {
        var rating30d: Double? = nil
        var ratingPrev: Double? = nil
        var reviews30d: Int? = nil
        var urgent: Int = 0
        var awaiting: Int = 0
        var responseRate: Int? = nil

        enum CodingKeys: String, CodingKey {
            case urgent, awaiting
            case rating30d = "rating_30d"
            case ratingPrev = "rating_prev"
            case reviews30d = "reviews_30d"
            case responseRate = "response_rate"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            rating30d = (try? c.decodeIfPresent(Double.self, forKey: .rating30d)) ?? nil
            ratingPrev = (try? c.decodeIfPresent(Double.self, forKey: .ratingPrev)) ?? nil
            reviews30d = (try? c.decodeIfPresent(Int.self, forKey: .reviews30d)) ?? nil
            urgent = ((try? c.decodeIfPresent(Int.self, forKey: .urgent)) ?? nil) ?? 0
            awaiting = ((try? c.decodeIfPresent(Int.self, forKey: .awaiting)) ?? nil) ?? 0
            responseRate = (try? c.decodeIfPresent(Int.self, forKey: .responseRate)) ?? nil
        }

        /// ↓ when the 30-day rating slipped 0.3★, ↑ when it rose 0.2★ — the
        /// web table's marks; nil otherwise.
        var trendMark: String? {
            guard let now = rating30d, let before = ratingPrev else { return nil }
            if now - before <= -0.3 { return "\u{2193}" }
            if now - before >= 0.2 { return "\u{2191}" }
            return nil
        }
    }

    /// Live labor % against the location's target; nil when no live shifts.
    struct Labor: Decodable {
        let pct: Double
        var over: Double = 0
        var overtime: Int = 0
        enum CodingKeys: String, CodingKey { case pct, over, overtime }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            pct = try c.decode(Double.self, forKey: .pct)
            over = ((try? c.decodeIfPresent(Double.self, forKey: .over)) ?? nil) ?? 0
            overtime = ((try? c.decodeIfPresent(Int.self, forKey: .overtime)) ?? nil) ?? 0
        }
    }

    /// Food cost: the measured % of sales and its target when the ledger
    /// has them (iOS re-audit M14), the recoverable dollars — an
    /// OPPORTUNITY, never a saving — and how many items are critically low;
    /// nil without live inventory.
    struct Inventory: Decodable {
        let recoverable: Double
        var criticalLow: Int = 0
        var pct: Double? = nil
        var target: Double? = nil
        enum CodingKeys: String, CodingKey {
            case recoverable, pct, target
            case criticalLow = "critical_low"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            recoverable = try c.decode(Double.self, forKey: .recoverable)
            criticalLow = ((try? c.decodeIfPresent(Int.self, forKey: .criticalLow)) ?? nil) ?? 0
            pct = (try? c.decodeIfPresent(Double.self, forKey: .pct)) ?? nil
            target = (try? c.decodeIfPresent(Double.self, forKey: .target)) ?? nil
        }
    }

    struct Issue: Decodable {
        let text: String
        let severity: String?
    }

    struct Location: Decodable, Identifiable {
        let id: Int
        let name: String
        /// "critical" | "important" | "watch" | "healthy".
        let health: String?
        /// How many things need the owner there.
        let attention: Int
        let lastNight: Night?
        // The group Home's per-location row (parity audit #32) — every one
        // lenient: an odd value is "—", never a screen that fails to open.
        var reviews: Reviews? = nil
        var labor: Labor? = nil
        var inventory: Inventory? = nil
        var issues: [Issue] = []
        var lastActive: String? = nil
        var active: Bool = false

        enum CodingKeys: String, CodingKey {
            case id, name, health, attention, reviews, labor, inventory, issues, active
            case lastNight = "last_night"
            case lastActive = "last_active"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(Int.self, forKey: .id)
            name = (try? c.decode(String.self, forKey: .name)) ?? ""
            health = (try? c.decodeIfPresent(String.self, forKey: .health)) ?? nil
            attention = ((try? c.decodeIfPresent(Int.self, forKey: .attention)) ?? nil) ?? 0
            lastNight = (try? c.decodeIfPresent(Night.self, forKey: .lastNight)) ?? nil
            reviews = (try? c.decodeIfPresent(Reviews.self, forKey: .reviews)) ?? nil
            labor = (try? c.decodeIfPresent(Labor.self, forKey: .labor)) ?? nil
            inventory = (try? c.decodeIfPresent(Inventory.self, forKey: .inventory)) ?? nil
            issues = (try? c.decodeIfPresent(HomeLenientListDecodable<Issue>.self, forKey: .issues))?.items ?? []
            lastActive = (try? c.decodeIfPresent(String.self, forKey: .lastActive)) ?? nil
            active = ((try? c.decodeIfPresent(Bool.self, forKey: .active)) ?? nil) ?? false
        }

        /// "2 need you · $8,420 net" — what the web switcher row says under
        /// the name; nil when there is nothing to say.
        var detail: String? {
            var bits: [String] = []
            if attention > 0 { bits.append("\(attention) need\(attention == 1 ? "s" : "") you") }
            if let net = lastNight?.net, net.isFinite { bits.append("$\(net.commaFormatted) net") }
            return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
        }
    }

    struct Attention: Decodable, Identifiable {
        let text: String
        let severity: String?
        let location: String?
        let restaurantId: Int?
        let nav: String?
        let actionLabel: String?

        var id: String { "\(restaurantId ?? 0)|\(text)" }

        enum CodingKeys: String, CodingKey {
            case text, severity, location, nav
            case restaurantId = "restaurant_id"
            case actionLabel = "action_label"
        }
    }

    struct Portfolio: Decodable {
        let total: Int?
        let healthy: Int?
        let needing: Int?
        let lastNight: Night?

        enum CodingKeys: String, CodingKey {
            case total, healthy, needing
            case lastNight = "last_night"
        }

        init(from decoder: Decoder) throws {
            let c = try? decoder.container(keyedBy: CodingKeys.self)
            total = (try? c?.decodeIfPresent(Int.self, forKey: .total)) ?? nil
            healthy = (try? c?.decodeIfPresent(Int.self, forKey: .healthy)) ?? nil
            needing = (try? c?.decodeIfPresent(Int.self, forKey: .needing)) ?? nil
            lastNight = (try? c?.decodeIfPresent(Night.self, forKey: .lastNight)) ?? nil
        }

        /// The group's strip (web `hbGroupTotal`): "3 of 4 healthy ·
        /// 9/24/26 $24,310 net across 3 of 4 · 1 needs a look".
        var line: String? {
            var bits: [String] = []
            if let h = healthy, let t = total { bits.append("\(h) of \(t) healthy") }
            if let n = lastNight, let net = n.net, net.isFinite {
                var s = (n.label.map { $0 + " " } ?? "") + "$\(net.commaFormatted) net"
                if let l = n.locations, let of = n.of { s += " across \(l) of \(of)" }
                bits.append(s)
            }
            if let needing, needing > 0 { bits.append("\(needing) need\(needing == 1 ? "s" : "") a look") }
            return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
        }
    }

    let ok: Bool
    let headline: String?
    let tone: String?
    let summaryLine: String?
    let locations: [Location]
    let attention: [Attention]
    let portfolio: Portfolio?
    var groupName: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, headline, tone, locations, attention, portfolio
        case summaryLine = "summary_line"
        case groupName = "group_name"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = ((try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? nil) ?? false
        headline = (try? c.decodeIfPresent(String.self, forKey: .headline)) ?? nil
        tone = (try? c.decodeIfPresent(String.self, forKey: .tone)) ?? nil
        summaryLine = (try? c.decodeIfPresent(String.self, forKey: .summaryLine)) ?? nil
        locations = ((try? c.decodeIfPresent([Location].self, forKey: .locations)) ?? nil) ?? []
        attention = ((try? c.decodeIfPresent(HomeLenientListDecodable<Attention>.self, forKey: .attention)) ?? nil)?.items ?? []
        portfolio = (try? c.decodeIfPresent(Portfolio.self, forKey: .portfolio)) ?? nil
        groupName = (try? c.decodeIfPresent(String.self, forKey: .groupName)) ?? nil
    }
}

/// The group Home's figures, pure so the rules are pinned by tests: a
/// missing measurement is "—", never 0; food cost is an opportunity, said so.
enum LocationGroupFormat {
    static let dash = "\u{2014}"

    /// "$8,420" and "+$310 vs budget" / "−$120 vs budget"; "not measured"
    /// for a finished night with no net; "—" with no night.
    static func lastNight(_ n: LocationGroupBrief.Night?) -> (figure: String, detail: String?) {
        guard let n else { return (dash, nil) }
        guard let net = n.net, net.isFinite else {
            let state = ["final", "provisional", "failed"].contains(n.status ?? "") ? "not measured" : "running"
            return (dash, state)
        }
        var detail: String? = nil
        if let vb = n.vsBudget, vb.isFinite {
            let r = vb.rounded()
            detail = (r > 0 ? "+" : (r < 0 ? "\u{2212}" : "")) + "$" + abs(r).commaFormatted + " vs budget"
        } else if let d = n.businessDate {
            detail = CavnarDate.mdy(d)
        }
        return ("$" + net.commaFormatted, detail)
    }

    /// "31.2%" and "+3.2 over" when more than 3 points over; "—" without
    /// live shifts.
    static func labor(_ l: LocationGroupBrief.Labor?) -> (figure: String, detail: String?) {
        guard let l else { return (dash, nil) }
        let pct = String(format: "%.1f%%", l.pct)
        if l.over > 3 { return (pct, String(format: "+%.1f over target", l.over)) }
        if l.over > 0 { return (pct, "over target") }
        return (pct, l.pct > 0 ? "on target" : nil)
    }

    /// "4.6★ ↓" and "3 urgent · 5 waiting"; "—" with no rating yet.
    static func reviews(_ r: LocationGroupBrief.Reviews?) -> (figure: String, detail: String?) {
        guard let r else { return (dash, nil) }
        let figure = r.rating30d.map { String(format: "%.1f\u{2605}", $0) + (r.trendMark.map { " " + $0 } ?? "") } ?? dash
        var bits: [String] = []
        if r.urgent > 0 { bits.append("\(r.urgent) urgent") }
        if r.awaiting > 0 { bits.append("\(r.awaiting) waiting") }
        return (figure, bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} "))
    }

    /// "34.2%" food cost and "vs 30% target · $1,240/mo could recover ·
    /// 2 low" (iOS re-audit M14): the figure is the measured %, "—" when
    /// the ledger has none — never the opportunity, which reads like money
    /// already measured. "—" without live inventory.
    static func foodCost(_ i: LocationGroupBrief.Inventory?) -> (figure: String, detail: String?) {
        guard let i else { return (dash, nil) }
        var bits: [String] = []
        if let t = i.target, t.isFinite, i.pct != nil { bits.append(String(format: "vs %g%% target", t)) }
        if i.recoverable > 0 { bits.append("$" + i.recoverable.commaFormatted + "/mo could recover") }
        if i.criticalLow > 0 { bits.append("\(i.criticalLow) low") }
        let figure = i.pct.flatMap { $0.isFinite ? String(format: "%.1f%%", $0) : nil } ?? dash
        return (figure, bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} "))
    }

    /// "2h ago" from the server's UTC stamp; "—" when nobody has signed in.
    static func lastActive(_ stamp: String?, now: Date = Date()) -> String {
        guard let stamp, let t = CavnarDate.timestamp(stamp) else { return dash }
        let m = max(0, Int(now.timeIntervalSince(t) / 60))
        if m < 1 { return "just now" }
        if m < 60 { return "\(m)m ago" }
        let h = m / 60
        if h < 24 { return "\(h)h ago" }
        let d = h / 24
        return d < 14 ? "\(d)d ago" : CavnarDate.mdy(t)
    }
}
