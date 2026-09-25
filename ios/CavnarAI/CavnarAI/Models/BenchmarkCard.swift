import Foundation

// MARK: - How you compare (benchmark_views.py over the Benchmark Engine)
//
// GET /mobile/api/benchmarks/card?module=labor|food_cost|reviews|marketing
// (and ?module=home for Home's strip), and /mobile/api/benchmarks/locations
// for the owner's locations side by side (Benchmarking audit 9/24/26, #23,
// #18, #19). Every word and figure is the server's: who the restaurant is
// compared to, how many, as of when, the comparison strength % and its Why?
// rows, the standing per metric, and the action for a metric it is behind
// on. Every field optional and every decoder lenient: an odd value is empty,
// never a screen that fails to load, and an older server that has no card
// draws nothing.

private extension KeyedDecodingContainer {
    func str(_ key: Key) -> String? {
        guard let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil else { return nil }
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }

    func int(_ key: Key) -> Int? {
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i }
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return Int(d.rounded()) }
        return nil
    }

    func bool(_ key: Key) -> Bool? { (try? decodeIfPresent(Bool.self, forKey: key)) ?? nil }
}

/// `who` — the comparison the card leads with: peers ("Compared to 11
/// other pizza restaurants on Cavnar"), self ("vs your own previous 13
/// weeks") or industry ("published: NRA 2025 …"), how many, as of when.
struct BenchmarkWho: Decodable, Hashable {
    var kind: String? = nil
    var text: String? = nil
    var n: Int? = nil
    var asOf: String? = nil
    var inferred: Bool = false

    enum CodingKeys: String, CodingKey { case kind, text, n, inferred; case asOf = "as_of" }

    init(kind: String? = nil, text: String? = nil, n: Int? = nil, asOf: String? = nil, inferred: Bool = false) {
        self.kind = kind; self.text = text; self.n = n; self.asOf = asOf; self.inferred = inferred
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        kind = c.str(.kind); text = c.str(.text); n = c.int(.n); asOf = c.str(.asOf)
        inferred = c.bool(.inferred) ?? false
    }

    /// "as of 9/20/26 · your type was guessed from your name" — nil when
    /// there is nothing to add under the headline.
    var subline: String? {
        var bits: [String] = []
        if kind == "peers", let asOf { bits.append("as of \(asOf)") }
        if kind == "self", let n { bits.append("\(n) weeks measured") }
        if kind == "self", let asOf { bits.append("through \(asOf)") }
        if inferred { bits.append("your type was guessed from your name") }
        return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
    }
}

/// The comparison strength of a band comparison — a percentage, never a
/// word — with the four rows its Why? sheet draws.
struct BenchmarkStrength: Decodable, Hashable {
    struct Row: Decodable, Hashable {
        var key: String? = nil
        var title: String = ""
        var pct: Int? = nil
        var basis: String? = nil

        enum CodingKeys: String, CodingKey { case key, title, pct, basis }

        init(key: String? = nil, title: String, pct: Int? = nil, basis: String? = nil) {
            self.key = key; self.title = title; self.pct = pct; self.basis = basis
        }

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            key = c.str(.key); title = c.str(.title) ?? ""; basis = c.str(.basis)
            pct = c.int(.pct).map { max(0, min(100, $0)) }
        }
    }

    var pct: Int? = nil
    var label: String? = nil
    var reason: String? = nil
    var meaning: String? = nil
    var footer: String? = nil
    var rows: [Row] = []

    enum CodingKeys: String, CodingKey { case pct, label, reason, meaning, footer, rows }

    init(pct: Int?, label: String? = nil, reason: String? = nil, meaning: String? = nil,
         footer: String? = nil, rows: [Row] = []) {
        self.pct = pct; self.label = label; self.reason = reason; self.meaning = meaning
        self.footer = footer; self.rows = rows
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        pct = c.int(.pct).map { max(0, min(100, $0)) }
        label = c.str(.label); reason = c.str(.reason); meaning = c.str(.meaning); footer = c.str(.footer)
        rows = (try? c.decodeIfPresent(DataHealthList<Row>.self, forKey: .rows))?.items ?? []
    }

    /// "72% comparison strength" — the server's label, else built from the %.
    var lineLabel: String? { label ?? pct.map { "\($0)% comparison strength" } }

    /// The Why? sheet's rows, in the confidence sheet's shape (the same
    /// ConfidenceDimensionRow draws both), toned by the same thresholds.
    var displayRows: [ConfidenceDisplay.Row] {
        rows.map { r in
            ConfidenceDisplay.Row(title: r.title, value: ConfidenceDisplay.percentText(r.pct),
                                  tone: ConfidenceDisplay.tone(pct: r.pct), basis: r.basis ?? "",
                                  detail: nil, meterFraction: ConfidenceDisplay.fraction(r.pct))
        }
    }
}

/// One metric's line on the card.
struct BenchmarkRow: Decodable, Hashable, Identifiable {
    struct Action: Decodable, Hashable {
        var label: String? = nil
        var ask: String? = nil
    }

    var metric: String = ""
    var label: String = ""
    var kind: String? = nil
    var valueText: String? = nil
    var standing: String? = nil
    /// good | neutral | warn — never red: a standing is not an emergency.
    var tone: String? = nil
    var behind: Bool = false
    var against: String? = nil
    var middleText: String? = nil
    var asOf: String? = nil
    var strengthPct: Int? = nil
    var note: String? = nil
    var action: Action? = nil
    /// A published figure measured differently — context, never a standing
    /// (Benchmarking #2).
    var context: Bool = false
    /// The data behind the figure is out of date: no standing (#25).
    var stale: Bool = false
    /// When the restaurant's OWN figure was measured, M/D/YY (#25).
    var ownAsOf: String? = nil
    /// "vs 9 like yours · 80% comparison strength" — the row's own group
    /// and strength, for the compact Home line (#26).
    var tag: String? = nil
    /// Where Home's Open goes (labor | inventory | reviews | marketing).
    var openModule: String? = nil

    var id: String { metric }

    enum CodingKeys: String, CodingKey {
        case metric, label, kind, standing, tone, behind, against, note, action, context, stale, tag
        case valueText = "value_text"
        case middleText = "middle_text"
        case asOf = "as_of"
        case strengthPct = "strength_pct"
        case ownAsOf = "own_as_of"
        case openModule = "open_module"
    }

    init(metric: String, label: String, kind: String? = nil, valueText: String? = nil, standing: String? = nil,
         tone: String? = nil, behind: Bool = false, against: String? = nil, middleText: String? = nil,
         asOf: String? = nil, strengthPct: Int? = nil, note: String? = nil, action: Action? = nil,
         context: Bool = false, stale: Bool = false, ownAsOf: String? = nil, tag: String? = nil,
         openModule: String? = nil) {
        self.metric = metric; self.label = label; self.kind = kind; self.valueText = valueText
        self.standing = standing; self.tone = tone; self.behind = behind; self.against = against
        self.middleText = middleText; self.asOf = asOf; self.strengthPct = strengthPct; self.note = note
        self.action = action; self.context = context; self.stale = stale; self.ownAsOf = ownAsOf
        self.tag = tag; self.openModule = openModule
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        metric = c.str(.metric) ?? ""; label = c.str(.label) ?? metric
        kind = c.str(.kind); valueText = c.str(.valueText); standing = c.str(.standing)
        tone = c.str(.tone); behind = c.bool(.behind) ?? false; against = c.str(.against)
        middleText = c.str(.middleText); asOf = c.str(.asOf); note = c.str(.note)
        strengthPct = c.int(.strengthPct).map { max(0, min(100, $0)) }
        action = (try? c.decodeIfPresent(Action.self, forKey: .action)) ?? nil
        context = c.bool(.context) ?? false; stale = c.bool(.stale) ?? false
        ownAsOf = c.str(.ownAsOf); tag = c.str(.tag); openModule = c.str(.openModule)
    }

    /// "Labor % 35% — worse than your normal" ("Food cost % 31%" for a
    /// context or out-of-date row, which carries no standing).
    var headline: String {
        [label + (valueText.map { " \($0)" } ?? ""), standing].compactMap { $0 }.joined(separator: " \u{2014} ")
    }

    /// "vs 12 other full-service restaurants (30.3%) · 80% comparison
    /// strength · your figure 9/20/26 · group as of 9/20/26" — what the
    /// standing is read against, its strength, and the dates of the
    /// restaurant's own figure and of the group (#25).
    var detail: String? {
        var bits: [String] = []
        if let against { bits.append((context ? "published: " : "vs ") + against + (middleText.map { " (\($0))" } ?? "")) }
        if let strengthPct { bits.append("\(strengthPct)% comparison strength") }
        if let ownAsOf { bits.append("your figure \(ownAsOf)") }
        if kind == "peers", let asOf {
            bits.append("group as of \(asOf)")
        } else if ownAsOf == nil, let asOf {
            bits.append("as of \(asOf)")
        }
        return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
    }
}

/// The honest state below the minimum: why there is no like-for-like group
/// (unconfirmed profile, spread, too few owners, the default wage, the
/// restaurant's own figure — #29), what the card falls back to, the
/// engine's reason, and for a profile reason the way to fix it.
struct BenchmarkBelowMinimum: Decodable, Hashable {
    struct Action: Decodable, Hashable {
        var kind: String? = nil
        var label: String? = nil
        var suggestion: String? = nil
        /// False for a login that may not change the profile: it is told
        /// who can instead of shown a button.
        var canEdit: Bool? = nil
        enum CodingKeys: String, CodingKey { case kind, label, suggestion; case canEdit = "can_edit" }
    }

    var text: String? = nil
    var whyNot: String? = nil
    var reason: String? = nil
    var action: Action? = nil
    enum CodingKeys: String, CodingKey { case text, reason, action; case whyNot = "why_not" }

    init(text: String? = nil, whyNot: String? = nil, reason: String? = nil, action: Action? = nil) {
        self.text = text; self.whyNot = whyNot; self.reason = reason; self.action = action
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        text = c.str(.text); whyNot = c.str(.whyNot); reason = c.str(.reason)
        action = (try? c.decodeIfPresent(Action.self, forKey: .action)) ?? nil
    }

    /// The "Confirm your profile" action, when there is one this login may take.
    var profileAction: Action? {
        guard let action, action.kind == "profile" else { return nil }
        return action
    }
}

/// The card (or the Home strip).
struct BenchmarkCard: Decodable, Hashable {
    var ok: Bool = false
    var error: String? = nil
    var who: BenchmarkWho? = nil
    var strength: BenchmarkStrength? = nil
    var rows: [BenchmarkRow] = []
    var belowMinimum: BenchmarkBelowMinimum? = nil
    var unmeasured: Int? = nil
    var empty: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, error, who, strength, rows, unmeasured, empty
        case belowMinimum = "below_minimum"
    }

    init(ok: Bool = true, who: BenchmarkWho? = nil, strength: BenchmarkStrength? = nil, rows: [BenchmarkRow] = [],
         belowMinimum: BenchmarkBelowMinimum? = nil, unmeasured: Int? = nil, empty: String? = nil) {
        self.ok = ok; self.who = who; self.strength = strength; self.rows = rows
        self.belowMinimum = belowMinimum; self.unmeasured = unmeasured; self.empty = empty
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        ok = c.bool(.ok) ?? false
        error = c.str(.error)
        who = (try? c.decodeIfPresent(BenchmarkWho.self, forKey: .who)) ?? nil
        strength = (try? c.decodeIfPresent(BenchmarkStrength.self, forKey: .strength)) ?? nil
        if strength?.pct == nil { strength = nil }
        rows = (try? c.decodeIfPresent(DataHealthList<BenchmarkRow>.self, forKey: .rows))?.items
            .filter { !$0.metric.isEmpty } ?? []
        belowMinimum = (try? c.decodeIfPresent(BenchmarkBelowMinimum.self, forKey: .belowMinimum)) ?? nil
        if belowMinimum?.text == nil { belowMinimum = nil }
        unmeasured = c.int(.unmeasured)
        empty = c.str(.empty)
    }

    /// Whether there is anything honest to draw: rows, or the reason there
    /// are none.
    var hasContent: Bool { ok && (!rows.isEmpty || empty != nil || belowMinimum != nil) }
}

// MARK: - Location to location

/// /mobile/api/benchmarks/locations — the owner's locations on the engine's
/// `location` kind: each against its own normal, then against the others,
/// a gap called only beyond noise.
struct LocationComparison: Decodable, Hashable {
    struct Location: Decodable, Hashable, Identifiable {
        var id: Int = 0
        var name: String = ""
        var valueText: String? = nil
        var vsOwn: String? = nil
        var vsGroup: String? = nil
        var tone: String? = nil
        var called: Bool = false
        var isThis: Bool = false
        var groupMedianText: String? = nil

        enum CodingKeys: String, CodingKey {
            case id, name, tone, called
            case valueText = "value_text"
            case vsOwn = "vs_own"
            case vsGroup = "vs_group"
            case isThis = "this"
            case groupMedianText = "group_median_text"
        }

        init(id: Int, name: String, valueText: String? = nil, vsOwn: String? = nil, vsGroup: String? = nil,
             tone: String? = nil, called: Bool = false, isThis: Bool = false, groupMedianText: String? = nil) {
            self.id = id; self.name = name; self.valueText = valueText; self.vsOwn = vsOwn; self.vsGroup = vsGroup
            self.tone = tone; self.called = called; self.isThis = isThis; self.groupMedianText = groupMedianText
        }

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            id = c.int(.id) ?? 0; name = c.str(.name) ?? ""
            valueText = c.str(.valueText); vsOwn = c.str(.vsOwn); vsGroup = c.str(.vsGroup)
            tone = c.str(.tone); called = c.bool(.called) ?? false; isThis = c.bool(.isThis) ?? false
            groupMedianText = c.str(.groupMedianText)
        }

        /// "its own normal: about its normal · others' middle 30.4%"
        var detail: String {
            var bits = [vsOwn.map { "vs its own normal: " + $0.replacingOccurrences(of: "your normal", with: "its normal") }
                        ?? "its own normal: not enough history yet"]
            if let groupMedianText { bits.append("others\u{2019} middle \(groupMedianText)") }
            return bits.joined(separator: " \u{00B7} ")
        }
    }

    struct Metric: Decodable, Hashable, Identifiable {
        var metric: String = ""
        var label: String = ""
        var locations: [Location] = []
        var id: String { metric }

        enum CodingKeys: String, CodingKey { case metric, label, locations }

        init(metric: String, label: String, locations: [Location]) {
            self.metric = metric; self.label = label; self.locations = locations
        }

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            metric = c.str(.metric) ?? ""; label = c.str(.label) ?? metric
            locations = (try? c.decodeIfPresent(DataHealthList<Location>.self, forKey: .locations))?.items ?? []
        }
    }

    var ok: Bool = false
    var metrics: [Metric] = []
    var whyNot: String? = nil

    enum CodingKeys: String, CodingKey { case ok, metrics; case whyNot = "why_not" }

    init(ok: Bool = true, metrics: [Metric] = [], whyNot: String? = nil) {
        self.ok = ok; self.metrics = metrics; self.whyNot = whyNot
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        ok = c.bool(.ok) ?? false
        metrics = (try? c.decodeIfPresent(DataHealthList<Metric>.self, forKey: .metrics))?.items
            .filter { !$0.locations.isEmpty } ?? []
        whyNot = c.str(.whyNot)
    }
}
