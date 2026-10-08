import Foundation

// Account health and the security checkup, scored on the server
// (account_health.py — iOS parity audit 10/7/26 #89 and the "Security
// checkup" matrix row). The app used to score its own seven checkup items
// with its own weights while the web scored six; now both draw the server's
// six, and the app adds this phone's re-entry lock beside them, outside the
// score (a setting of the phone, not of the account). Every field decodes
// leniently: a missing measurement is "—", never 0.

/// GET /mobile/api/account/security-summary.
struct SecuritySummary: Decodable {
    var ok = false
    var checkup = SecurityCheckup()

    enum CodingKeys: String, CodingKey { case ok, checkup }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.setupBool(.ok) ?? false
        checkup = (try? c.decodeIfPresent(SecurityCheckup.self, forKey: .checkup)) ?? nil ?? SecurityCheckup()
    }
}

/// The six shared checkup items and their score — account_health.security_checkup.
struct SecurityCheckup: Decodable, Equatable {
    var score: Int?
    var max = 100
    var items: [Item] = []

    struct Item: Decodable, Equatable, Identifiable {
        let key: String
        let title: String
        let points: Int
        let earned: Bool
        let detail: String
        /// The action's words when the item isn't earned ("Turn on").
        let fixLabel: String?
        var id: String { key }

        enum CodingKeys: String, CodingKey { case key, title, points, earned, detail; case fixLabel = "fix_label" }

        init(key: String, title: String, points: Int, earned: Bool, detail: String, fixLabel: String?) {
            self.key = key; self.title = title; self.points = points; self.earned = earned
            self.detail = detail; self.fixLabel = fixLabel
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = c.setupText(.key) ?? ""
            title = c.setupText(.title) ?? ""
            points = c.setupInt(.points) ?? 0
            earned = c.setupBool(.earned) ?? false
            detail = c.setupText(.detail) ?? ""
            fixLabel = c.setupText(.fixLabel)
        }
    }

    enum CodingKeys: String, CodingKey { case score, max, items }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        score = c.setupInt(.score)
        max = c.setupInt(.max) ?? 100
        items = c.setupList(Item.self, .items).filter { !$0.key.isEmpty }
    }
}

/// GET /mobile/api/account/health — account_health.payload.
struct AccountHealth: Decodable {
    var ok = false
    var score: Int?
    /// good | warn | bad — the ring's colour (90+, 60–89, under 60).
    var tone = "warn"
    var sub: String?
    var sayLead: String?
    var sayText: String?
    var fix: Fix?
    var items: [Item] = []
    var features: [Feature] = []
    var measured: Measured?
    var connected: Int?
    var connectionsTotal: Int?

    struct Fix: Decodable, Equatable {
        let key: String
        let label: String
    }

    struct Item: Decodable, Equatable, Identifiable {
        let key: String
        let label: String
        /// ok | warn | bad
        let state: String
        let sub: String
        var id: String { key }
        enum CodingKeys: String, CodingKey { case key, label, state, sub }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = c.setupText(.key) ?? ""
            label = c.setupText(.label) ?? ""
            state = c.setupText(.state) ?? "warn"
            sub = c.setupText(.sub) ?? ""
        }
    }

    /// One module the plan may carry, and whether it does.
    struct Feature: Decodable, Equatable, Identifiable {
        let key: String
        let label: String
        let detail: String
        let on: Bool
        var id: String { key }
        enum CodingKeys: String, CodingKey { case key, label, detail, on }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = c.setupText(.key) ?? ""
            label = c.setupText(.label) ?? ""
            detail = c.setupText(.detail) ?? ""
            on = c.setupBool(.on) ?? false
        }
    }

    /// The measured-value line — value_delivered.delivered only, net of
    /// what got worse. Never an opportunity, surfaced or avoided figure
    /// (CLAUDE.md "Value delivered"); `netMonthly` is nil until something
    /// was measured.
    struct Measured: Decodable, Equatable {
        let line: String?
        let netMonthly: Double?
        let measured: Bool
        enum CodingKeys: String, CodingKey { case line, measured; case netMonthly = "net_monthly" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            line = c.setupText(.line)
            netMonthly = c.setupDouble(.netMonthly)
            measured = c.setupBool(.measured) ?? false
        }
    }

    private struct Say: Decodable { let lead: String?; let text: String? }
    private struct Connections: Decodable { let connected: Int?; let total: Int? }

    enum CodingKeys: String, CodingKey { case ok, score, tone, sub, say, fix, items, features, measured, connections }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.setupBool(.ok) ?? false
        score = c.setupInt(.score)
        tone = c.setupText(.tone) ?? "warn"
        sub = c.setupText(.sub)
        let say = (try? c.decodeIfPresent(Say.self, forKey: .say)) ?? nil
        sayLead = say?.lead.flatMap { $0.isEmpty ? nil : $0 }
        sayText = say?.text.flatMap { $0.isEmpty ? nil : $0 }
        fix = (try? c.decodeIfPresent(Fix.self, forKey: .fix)) ?? nil
        items = c.setupList(Item.self, .items).filter { !$0.key.isEmpty }
        features = c.setupList(Feature.self, .features).filter { !$0.key.isEmpty }
        measured = (try? c.decodeIfPresent(Measured.self, forKey: .measured)) ?? nil
        let conn = (try? c.decodeIfPresent(Connections.self, forKey: .connections)) ?? nil
        connected = conn?.connected
        connectionsTotal = conn?.total
    }

    /// "Nearly set up. Worth doing: …" — the one sentence.
    var sentence: String? {
        let parts = [sayLead, sayText].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }
}
