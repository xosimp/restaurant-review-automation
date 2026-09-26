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

        enum CodingKeys: String, CodingKey {
            case net, label, locations, of
            case vsBudget = "vs_budget"
        }

        init(from decoder: Decoder) throws {
            let c = try? decoder.container(keyedBy: CodingKeys.self)
            net = (try? c?.decodeIfPresent(Double.self, forKey: .net)) ?? nil
            label = (try? c?.decodeIfPresent(String.self, forKey: .label)) ?? nil
            locations = (try? c?.decodeIfPresent(Int.self, forKey: .locations)) ?? nil
            of = (try? c?.decodeIfPresent(Int.self, forKey: .of)) ?? nil
            vsBudget = (try? c?.decodeIfPresent(Double.self, forKey: .vsBudget)) ?? nil
        }
    }

    struct Location: Decodable, Identifiable {
        let id: Int
        let name: String
        /// "critical" | "important" | "watch" | "healthy".
        let health: String?
        /// How many things need the owner there.
        let attention: Int
        let lastNight: Night?

        enum CodingKeys: String, CodingKey {
            case id, name, health, attention
            case lastNight = "last_night"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(Int.self, forKey: .id)
            name = (try? c.decode(String.self, forKey: .name)) ?? ""
            health = (try? c.decodeIfPresent(String.self, forKey: .health)) ?? nil
            attention = ((try? c.decodeIfPresent(Int.self, forKey: .attention)) ?? nil) ?? 0
            lastNight = (try? c.decodeIfPresent(Night.self, forKey: .lastNight)) ?? nil
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

    enum CodingKeys: String, CodingKey {
        case ok, headline, tone, locations, attention, portfolio
        case summaryLine = "summary_line"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = ((try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? nil) ?? false
        headline = (try? c.decodeIfPresent(String.self, forKey: .headline)) ?? nil
        tone = (try? c.decodeIfPresent(String.self, forKey: .tone)) ?? nil
        summaryLine = (try? c.decodeIfPresent(String.self, forKey: .summaryLine)) ?? nil
        locations = ((try? c.decodeIfPresent([Location].self, forKey: .locations)) ?? nil) ?? []
        attention = ((try? c.decodeIfPresent([Attention].self, forKey: .attention)) ?? nil) ?? []
        portfolio = (try? c.decodeIfPresent(Portfolio.self, forKey: .portfolio)) ?? nil
    }
}
