import Foundation

/// Decodes GET /mobile/api/home — a deliberately trimmed aggregate (see
/// mobile_api.py's _do_mobile_home docstring): just the KPI numbers an
/// owner glances at and the same "needs attention" list the web Home tab
/// shows, not the full desktop dashboard's savings-breakdown/onboarding/
/// marketing-agency-value machinery.
///
/// `modules` is a generic array (models.get_active_modules() on the
/// backend), not a fixed set of named fields — this is what lets Home and
/// the Modules tab render any number of modules (today: 5; tomorrow:
/// Waitlist, Bar & Alcohol, whatever else) without an app update just to
/// show a new module's tile.
struct HomeSummary: Codable {
    let username: String?
    let restaurantName: String
    let locationName: String?
    let brandColor: String?
    let reviewsAwaitingApproval: Int
    let modules: [ModuleSummary]
    let needsAttention: [NeedsAttentionItem]
    /// A MONTHLY run-rate of measured improvements (value_delivered.headline),
    /// not a lifetime total — H-8.
    let totalValueDelivered: Int
    let valueHistory: [ValueSnapshot]
    /// "measured, per month", and the modules the figure was measured on,
    /// largest first. Optional: an older server omits them.
    let valueLabel: String?
    let valueByModule: [ValueModulePart]?
    /// What got worse beside the improvements, the net, the measured-days
    /// sum and the unpriced wins (value_delivered.headline). Optional: an
    /// older server sends none, and the band shows `totalValueDelivered`
    /// exactly as before. See `valueHeadline`.
    let value: HomeValueBlock?
    // Computed server-side by the exact same is_in_quiet_hours() check
    // notify.py's own alert dispatch gates on, so the Home badge can never
    // disagree with what's actually being held back right now.
    let quietHoursActive: Bool
    let alertQuietEnd: String?
    // Home's hero subline ("Overnight, Cavnar answered 3 reviews and
    // flagged 2 things for you") and its closing "This week" receipt — see
    // mobile_api.py's _home_overnight / _home_weekly_receipts. Both optional
    // on purpose: a summary cached before these shipped still decodes, and
    // the hero simply falls back to its quiet line.
    let overnight: HomeOvernight?
    let weeklyReceipts: [HomeWeeklyReceipt]?
    // The web Home's getting-started card. Empty once every step is done
    // or the owner dismissed it — see mobile_api._setup_checklist.
    let setupChecklist: [HomeSetupStep]?
    // What Cavnar recommends, from home_brief. The server has sent these on
    // every /mobile/api/home response since Home shipped and the app never
    // decoded them, so the one button that starts an outcome tracker
    // ("Track this") existed only on the web — and the owner is on the
    // phone. Without a tracker nothing ever reaches outcomes.record, which
    // is what eventually produces "that one worked, about $420/month".
    let recommendations: [HomeRecommendation]?
    // The first session only. Google's own rating and how it sits against
    // the comparable restaurants nearest this one, for the screen where
    // every other block is empty by definition. The server returns [] the
    // moment the account has data of its own.
    let firstLook: [String]?
    // What is connected and what the product can therefore measure. Per
    // module: connected, what is readable right now, and — when it is not
    // — the one action that would light it up. Never a score.
    let readiness: HomeReadiness?
    /// The restaurant's own clock (ISO), for the day's slot: the close-out
    /// leads it after 8pm, the weekly receipts on Monday. Optional — an
    /// older server omits it and the brief simply leads.
    let localNow: String?
    /// Recommendation kinds that went quieter after the last four passed
    /// unanswered (home_brief / decisions.quiet_kinds), with a way back.
    let quieter: [HomeQuietKind]?
    /// Who a card can be handed to — consented alert contacts; empty for a
    /// login that may not open issues.
    let assignees: [HomeAssignee]?
    /// K4/J2 — how current each source behind Home is, the stalest date
    /// (`data_as_of`) and how many sources are live (`monitoring`). All
    /// lenient: an odd value is empty, never a Home that fails to decode;
    /// an older server omits them and the strip doesn't draw.
    var freshness: HomeFreshnessList? = nil
    var dataAsOf: LenientText? = nil
    var monitoring: HomeMonitoring? = nil
    /// The Restaurant Data Health Score beside the legacy list
    /// (`data_health: {overall, worst_line}`, may be null), and whether the
    /// server could not work out freshness at all — `freshness` is then
    /// empty, and the strip says so instead of drawing nothing. Both
    /// lenient; an older server omits them.
    var dataHealth: HomeDataHealth? = nil
    var freshnessUnavailableFlag: LenientFlag? = nil
    /// Web Home's H1 — home_brief's headline and its tone — which the hero
    /// renders in place of a slogan (density #1). Lenient; nil on an older
    /// server or when the brief couldn't be built, and the hero keeps its
    /// greeting line.
    var brief: HomeBriefHead? = nil
    /// The rest of what web Home reads from the brief (parity audit #1),
    /// now on /mobile/api/home too. Every one lenient — an odd value is
    /// empty, never a Home that fails to decode — and absent from an older
    /// server or a summary cached before them.
    /// The header's one-tap actions, ranked server-side (home_brief).
    var quickActions: HomeLenientList<HomeQuickAction>? = nil
    /// "Since your last visit: …" — what changed since this login last
    /// looked.
    var changes: HomeChanges? = nil
    /// The trend behind each pulse chip: the weekly rating, labor by
    /// weekday against target, last week's waste by item.
    var charts: HomeCharts? = nil
    /// Recommendations this login hid, newest first — the undo.
    var dismissed: HomeLenientList<HomeDismissedRec>? = nil
    /// What went right (home_brief wins). Decoded for parity; web Home
    /// counts them in the brief and draws no list of its own, so neither
    /// does the phone.
    var wins: HomeLenientList<HomeWin>? = nil

    var freshnessUnavailable: Bool { freshnessUnavailableFlag?.value == true }

    var localHour: Int? {
        guard let s = localNow, let t = s.firstIndex(of: "T") else { return nil }
        return Int(s[s.index(after: t)..<s.index(t, offsetBy: 3)])
    }
    var localIsEvening: Bool { (localHour ?? 12) >= 20 }
    var localIsMonday: Bool {
        guard let s = localNow, s.count >= 10 else { return false }
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = TimeZone(secondsFromGMT: 0)
        guard let d = f.date(from: String(s.prefix(10))) else { return false }
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(secondsFromGMT: 0)!
        return cal.component(.weekday, from: d) == 2
    }
    /// Nothing connected yet: readiness is the page, so it leads.
    var isFresh: Bool {
        guard let r = readiness, !r.modules.isEmpty else { return false }
        return !r.modules.contains { $0.connected }
    }

    enum CodingKeys: String, CodingKey {
        case username
        case restaurantName = "restaurant_name"
        case locationName = "location_name"
        case brandColor = "brand_color"
        case reviewsAwaitingApproval = "reviews_awaiting_approval"
        case modules
        case needsAttention = "needs_attention"
        case totalValueDelivered = "total_value_delivered"
        case valueLabel = "value_label"
        case valueByModule = "value_by_module"
        case value
        case valueHistory = "value_history"
        case quietHoursActive = "quiet_hours_active"
        case alertQuietEnd = "alert_quiet_end"
        case overnight
        case weeklyReceipts = "weekly_receipts"
        case setupChecklist = "setup_checklist"
        case recommendations
        case firstLook = "first_look"
        case readiness
        case localNow = "local_now"
        case quieter, assignees, freshness, monitoring
        case dataAsOf = "data_as_of"
        case dataHealth = "data_health"
        case freshnessUnavailableFlag = "freshness_unavailable"
        case brief
        case quickActions = "quick_actions"
        case changes, charts, dismissed, wins
    }

    /// "9/22/26" — `data_as_of` in the owner's format whether the server
    /// sent M/D/YY or ISO; nil when absent or unreadable.
    var dataAsOfDisplay: String? {
        ConfidenceDisplay.mdyDate(asOf: dataAsOf?.value, asOfISO: nil)
    }
}

// MARK: - Freshness (K4 / J2)

/// One source behind Home and how current it is. The K4 shape is
/// `{module, source, state, pct, as_of, basis}` with state current | aging
/// | stale | not_connected | unknown | sample; an older server sent
/// `{key, label, at, state: fresh|stale|missing|manual|sample, note}`.
/// Both read; every field lenient.
struct HomeFreshnessEntry: Codable, Hashable, Identifiable {
    enum State: String, Codable, Hashable {
        /// `disconnected`: a POS removed after use keeps its last data's
        /// recency (data_freshness); it reads as a warning, never current.
        case current, aging, stale, notConnected, unknown, sample, disconnected
    }

    let module: String?
    /// The data_freshness source KEY ("pos", "shifts", "reviews") — an id,
    /// never printed: the strip prints `label` (B6#9).
    let source: String?
    /// The owner's name for the row ("Labor", "Food cost"), as the server
    /// wrote it.
    let label: String?
    let state: State
    let pct: Int?
    let asOf: String?
    let basis: String?

    var id: String { (module ?? "") + "|" + (source ?? label ?? "") }

    enum CodingKeys: String, CodingKey {
        case module, source, state, pct, basis, key, label, at, note
        case asOf = "as_of"
    }

    init(module: String?, source: String?, label: String? = nil, state: State, pct: Int? = nil,
         asOf: String? = nil, basis: String? = nil) {
        self.module = module; self.source = source; self.label = label; self.state = state
        self.pct = pct; self.asOf = asOf; self.basis = basis
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func text(_ k: CodingKeys) -> String? {
            guard let s = (try? c.decodeIfPresent(String.self, forKey: k)) ?? nil else { return nil }
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? nil : t
        }
        module = text(.module) ?? text(.key)
        source = text(.source)
        label = text(.label)
        basis = text(.basis) ?? text(.note)
        let rawAsOf = text(.asOf) ?? text(.at)
        asOf = rawAsOf.flatMap { ConfidenceDisplay.mdyDate(asOf: $0, asOfISO: nil) }
        if let i = (try? c.decodeIfPresent(Int.self, forKey: .pct)) ?? nil {
            pct = max(0, min(100, i))
        } else if let d = (try? c.decodeIfPresent(Double.self, forKey: .pct)) ?? nil, d.isFinite {
            pct = max(0, min(100, Int(d.rounded())))
        } else {
            pct = nil
        }
        state = Self.state(text(.state), hasAge: rawAsOf != nil)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(module, forKey: .module)
        try c.encodeIfPresent(source, forKey: .source)
        try c.encodeIfPresent(label, forKey: .label)
        try c.encode(Self.wire(state), forKey: .state)
        try c.encodeIfPresent(pct, forKey: .pct)
        try c.encodeIfPresent(asOf, forKey: .asOf)
        try c.encodeIfPresent(basis, forKey: .basis)
    }

    /// Both vocabularies onto one. Unknown age is never current: an old
    /// "fresh" with no date reads "unknown".
    static func state(_ raw: String?, hasAge: Bool) -> State {
        switch raw?.lowercased() {
        case "current": return .current
        case "fresh": return hasAge ? .current : .unknown
        case "aging": return .aging
        case "stale": return .stale
        case "not_connected", "missing", "manual": return .notConnected
        case "sample": return .sample
        case "disconnected": return .disconnected
        default: return .unknown
        }
    }

    static func wire(_ s: State) -> String {
        switch s {
        case .current: return "current"
        case .aging: return "aging"
        case .stale: return "stale"
        case .notConnected: return "not_connected"
        case .unknown: return "unknown"
        case .sample: return "sample"
        case .disconnected: return "disconnected"
        }
    }

    /// The row's name as the strip prints it: the server's label, else the
    /// module's own name. Never the source key — "pos" and "shifts" are ids
    /// (B6#9: the chips read "pos", "labor").
    var name: String {
        if let label { return label }
        guard let module else { return "Data" }
        return module == "inventory" ? "Food cost" : module.prefix(1).uppercased() + module.dropFirst()
    }

    /// What the chip says after the name: the basis, else what the state
    /// means ("not connected", "age unknown", "sample data").
    var caption: String? {
        if let basis { return basis }
        switch state {
        case .notConnected: return "not connected"
        case .unknown: return "age unknown"
        case .sample: return "sample data"
        case .disconnected: return "disconnected"
        case .current, .aging, .stale: return asOf.map { "as of " + $0 }
        }
    }
}

/// `freshness` read element by element: an entry that isn't an object is
/// skipped, and a value that isn't a list is empty — never a failed Home.
struct HomeFreshnessList: Codable, Hashable {
    let entries: [HomeFreshnessEntry]

    init(_ entries: [HomeFreshnessEntry]) { self.entries = entries }

    init(from decoder: Decoder) throws {
        guard var list = try? decoder.unkeyedContainer() else { entries = []; return }
        var out: [HomeFreshnessEntry] = []
        while !list.isAtEnd {
            let before = list.currentIndex
            if let e = try? list.decode(HomeFreshnessEntry.self) {
                out.append(e)
            } else {
                // Step over the odd element (any JSON value).
                _ = try? list.decode(JSONValue.self)
            }
            if list.currentIndex == before { break }
        }
        entries = out
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(entries)
    }
}

/// `monitoring: {count_live, stalest_as_of, stale, all_clear}` — how many
/// sources are current, the stalest one's date, how many read stale or
/// unknown, and whether an "All clear" may be drawn at all (home_brief:
/// nothing flagged, a live source, none stale). Lenient: `stale` and
/// `all_clear` are nil from an older server.
/// `brief` on GET /mobile/api/home: `{headline, tone}` from home_brief —
/// "2 things need you now" / "bad". Every field lenient; an unreadable
/// headline decodes as nil and the hero keeps its greeting.
struct HomeBriefHead: Codable, Hashable {
    let headline: String?
    let tone: String?

    init(headline: String?, tone: String?) {
        self.headline = headline; self.tone = tone
    }

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        headline = (try? c?.decodeIfPresent(String.self, forKey: .headline)) ?? nil
        tone = (try? c?.decodeIfPresent(String.self, forKey: .tone)) ?? nil
    }

    enum CodingKeys: String, CodingKey { case headline, tone }
}

struct HomeMonitoring: Codable, Hashable {
    let countLive: Int?
    let stalestAsOf: String?
    var stale: Int? = nil
    var allClear: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case countLive = "count_live"
        case stalestAsOf = "stalest_as_of"
        case stale
        case allClear = "all_clear"
    }

    init(countLive: Int?, stalestAsOf: String?, stale: Int? = nil, allClear: Bool? = nil) {
        self.countLive = countLive; self.stalestAsOf = stalestAsOf
        self.stale = stale; self.allClear = allClear
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else {
            countLive = nil; stalestAsOf = nil; return
        }
        countLive = try? c.decodeIfPresent(Int.self, forKey: .countLive)
        let s = (try? c.decodeIfPresent(String.self, forKey: .stalestAsOf)) ?? nil
        stalestAsOf = s.flatMap { ConfidenceDisplay.mdyDate(asOf: $0, asOfISO: nil) }
        stale = (try? c.decodeIfPresent(Int.self, forKey: .stale)) ?? nil
        allClear = (try? c.decodeIfPresent(Bool.self, forKey: .allClear)) ?? nil
    }
}

struct HomeQuietKind: Codable, Hashable, Identifiable {
    let kind: String
    let label: String
    var id: String { kind }
}

struct HomeAssignee: Codable, Hashable, Identifiable {
    let id: Int
    let name: String
}

/// home_brief.readiness: which modules have data flowing and what that
/// makes measurable. `complete` hides the card — it exists to name the next
/// step, not to grade a finished setup.
struct HomeReadiness: Codable {
    struct Module: Codable, Identifiable {
        let key: String
        let label: String
        let connected: Bool
        let measurable: [String]?
        let next: String?
        let module: String?
        var id: String { key }
    }
    let modules: [Module]
    let connected: Int?
    let total: Int?
    let complete: Bool?
}

/// One line from home_brief's "Cavnar recommends". `metric` is what makes it
/// trackable: a recommendation that names a metric can be measured before
/// and after, and one that doesn't can only be read.
struct HomeRecommendation: Codable, Identifiable, Hashable {
    let key: String
    /// What to do, verb first.
    let title: String
    /// Why now.
    let why: String?
    let evidence: String?
    let module: String?
    let metric: String?
    /// ONE confidence for the card (K1: the percentage, what it rests on,
    /// and the three dimensions behind "Why?"). Optional: older servers
    /// omit it or send the band-only object, and both still render.
    let confidence: TrustConfidence?
    let timeframe: String?
    let impact: String?
    /// The old "evidence strength" pill's key. Decoded for an older server
    /// or a cached summary and deliberately not rendered: the confidence
    /// line replaced it (CA4 F1 — two strength readings on one card). New
    /// servers leave the key out.
    let strength: String?
    /// Dollars a month at stake — only when measured, never invented.
    let dollarsMonthly: Double?
    let ifIgnored: String?
    let alternative: String?
    /// A one-tap finish (a reprice at the suggested price), when there is one.
    let action: HomeRecAction?
    let timesHidden: Int?
    /// True when the model wrote the card's words (K4) — a small
    /// "AI-written" tag.
    let modelWritten: Bool?
    /// F6: the dollars corrected by this restaurant's measured results
    /// (RecDollarCalibration). Absent on an older server.
    var dollarsAdjusted: Double? = nil
    var calibrationN: Int? = nil
    var calibrationNote: String? = nil
    /// What the dollar figure covers ("one Tuesday's overstaffing, per
    /// month"): one Home can carry three labor figures of different scope
    /// (B4 H7). Absent on an older server.
    var dollarsBasis: String? = nil
    /// The money figure's kind (measured / opportunity / projection / ...)
    /// when the server sends one — it picks the chip's word
    /// (OwnerCopy.kindWord). Absent on an older server: "at stake".
    var dollarsKind: String? = nil
    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, title, why, evidence, module, metric, confidence, timeframe, impact, strength, alternative, action
        case dollarsMonthly = "dollars_monthly"
        case dollarsBasis = "dollars_basis"
        case dollarsKind = "dollars_kind"
        case ifIgnored = "if_ignored"
        case timesHidden = "times_hidden"
        case modelWritten = "model_written"
        case dollarsAdjusted = "dollars_adjusted"
        case calibrationN = "calibration_n"
        case calibrationNote = "calibration_note"
    }

    /// The at-stake figure the card states — the calibrated one when sent.
    var statedDollars: Double? { RecDollarCalibration.figure(raw: dollarsMonthly, adjusted: dollarsAdjusted) }
    /// "adjusted from 6 measured results", beside the figure, when it was.
    var dollarsNote: String? {
        RecDollarCalibration.note(adjusted: dollarsAdjusted, n: calibrationN, note: calibrationNote)
    }
}

extension HomeRecommendation {
    /// `key` and `title` are the card; everything else is read leniently —
    /// an odd value is nil, never a Home that fails to decode.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        title = try c.decode(String.self, forKey: .title)
        why = try? c.decodeIfPresent(String.self, forKey: .why)
        evidence = try? c.decodeIfPresent(String.self, forKey: .evidence)
        module = try? c.decodeIfPresent(String.self, forKey: .module)
        metric = try? c.decodeIfPresent(String.self, forKey: .metric)
        confidence = try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence)
        timeframe = try? c.decodeIfPresent(String.self, forKey: .timeframe)
        impact = try? c.decodeIfPresent(String.self, forKey: .impact)
        strength = try? c.decodeIfPresent(String.self, forKey: .strength)
        dollarsMonthly = try? c.decodeIfPresent(Double.self, forKey: .dollarsMonthly)
        ifIgnored = try? c.decodeIfPresent(String.self, forKey: .ifIgnored)
        alternative = try? c.decodeIfPresent(String.self, forKey: .alternative)
        action = try? c.decodeIfPresent(HomeRecAction.self, forKey: .action)
        timesHidden = try? c.decodeIfPresent(Int.self, forKey: .timesHidden)
        modelWritten = try? c.decodeIfPresent(Bool.self, forKey: .modelWritten)
        dollarsAdjusted = try? c.decodeIfPresent(Double.self, forKey: .dollarsAdjusted)
        calibrationN = try? c.decodeIfPresent(Int.self, forKey: .calibrationN)
        calibrationNote = try? c.decodeIfPresent(String.self, forKey: .calibrationNote)
        dollarsBasis = RecDollarCalibration.basis((try? c.decodeIfPresent(String.self, forKey: .dollarsBasis)) ?? nil)
        dollarsKind = (try? c.decodeIfPresent(String.self, forKey: .dollarsKind)) ?? nil
    }
}

/// The dollar calibration a recommendation may carry (F6,
/// rec_learning.attach_dollar_calibration — Home cards, the one thing, DSR
/// actions): `dollars_adjusted` is the figure corrected by this restaurant's
/// measured results for the kind (null: show `dollars_monthly` as it is),
/// `calibration_n` how many results, `calibration_note` the server's words.
/// Pure, so the rule is pinned by tests.
enum RecDollarCalibration {
    /// "covers the whole schedule's gap to your target, per month" — what a
    /// dollar figure covers, as the server wrote it (dollars_basis), or nil.
    static func basis(_ raw: String?) -> String? {
        guard var b = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !b.isEmpty else { return nil }
        if b.hasSuffix(".") { b.removeLast() }
        return b.lowercased().hasPrefix("covers") ? b : "covers " + b
    }

    /// The figure to show: the adjusted one when the server calibrated it,
    /// else the raw one; nil when neither is above zero.
    static func figure(raw: Double?, adjusted: Double?) -> Double? {
        if let a = adjusted, a > 0 { return a }
        if adjusted != nil { return nil }
        guard let r = raw, r > 0 else { return nil }
        return r
    }

    /// "adjusted from 6 measured results" — only when the figure WAS
    /// adjusted; the server's note first, else built from the count.
    static func note(adjusted: Double?, n: Int?, note: String?) -> String? {
        guard adjusted != nil else { return nil }
        if let t = note?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty { return t }
        guard let n, n > 0 else { return nil }
        return "adjusted from \(n) measured result\(n == 1 ? "" : "s")"
    }

    /// "$1,240/mo · adjusted from 6 measured results", or the raw "$1,500/mo".
    static func line(raw: Double?, adjusted: Double?, n: Int?, note: String?) -> String? {
        guard let f = figure(raw: raw, adjusted: adjusted) else { return nil }
        let base = "$\(f.commaFormatted)/mo"
        return self.note(adjusted: adjusted, n: n, note: note).map { base + " \u{00B7} " + $0 } ?? base
    }
}

struct HomeRecAction: Codable, Hashable {
    let kind: String
    let dish: String?
    let price: Double?
    let label: String?
    let count: Int?
}

// The card's confidence is `TrustConfidence` (Models/TrustConfidence.swift),
// which still decodes the older {score, band, label, reason, caution}
// object; `HomeConfidence` is kept there as an alias.

struct HomeSetupStep: Codable, Identifiable, Hashable {
    let key: String
    let label: String
    let sub: String?
    let done: Bool
    let module: String?
    var id: String { key }
}

/// Drafts written and alerts fired in the last `windowHours` — the numbers
/// the hero subline is built from.
struct HomeOvernight: Codable, Hashable {
    let answered: Int
    let flagged: Int
    let windowHours: Int?

    enum CodingKeys: String, CodingKey {
        case answered, flagged
        case windowHours = "window_hours"
    }
}

/// One line of the "This week — what Cavnar did for you" receipt: a bold
/// `emphasis` ("9 replies") followed by the rest of the sentence. The
/// backend only sends lines whose number is non-zero, so an empty list
/// means the section is hidden, never padded.
struct HomeWeeklyReceipt: Codable, Identifiable, Hashable {
    let module: String
    let emphasis: String
    let text: String

    var id: String { module + "|" + emphasis + "|" + text }
}

/// One day's measured-results figure — see value_delivered.py's
/// record_value_snapshot(). Ascending by date, oldest first.
struct ValueSnapshot: Codable, Hashable {
    let date: String
    let value: Int
}

/// One module's share of the measured monthly value (value_delivered.headline).
struct ValueModulePart: Codable, Hashable {
    let module: String
    let label: String
    let monthly: Double
}

/// `value` on GET /mobile/api/home — the headline's other half: what got
/// worse, the net of it, the measured-days sum, and the wins with no dollar
/// rate. Every field optional and read leniently (an odd value is nil,
/// never a Home that fails to decode); a cached summary from before it
/// shipped simply has none.
struct HomeValueBlock: Codable, Hashable {
    struct Worsened: Codable, Hashable {
        let count: Int?
        let monthly: Double?
        /// The worse results carrying dollars — the only ones `monthly` covers.
        let pricedCount: Int?
        enum CodingKeys: String, CodingKey {
            case count, monthly
            case pricedCount = "priced_count"
        }
        init(count: Int?, monthly: Double?, pricedCount: Int?) {
            self.count = count
            self.monthly = monthly
            self.pricedCount = pricedCount
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            count = try? c.decodeIfPresent(Int.self, forKey: .count)
            monthly = try? c.decodeIfPresent(Double.self, forKey: .monthly)
            pricedCount = try? c.decodeIfPresent(Int.self, forKey: .pricedCount)
        }
    }
    /// A SUM of measured days (never monthly × months); `total` null is
    /// "nothing measured", not $0.
    struct Cumulative: Codable, Hashable {
        let total: Double?
        let gained: Double?
        let lost: Double?
        let since: String?
        let until: String?
        let days: Int?
        let measuredDays: Int?
        let basis: String?
        enum CodingKeys: String, CodingKey {
            case total, gained, lost, since, until, days, basis
            case measuredDays = "measured_days"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            total = try? c.decodeIfPresent(Double.self, forKey: .total)
            gained = try? c.decodeIfPresent(Double.self, forKey: .gained)
            lost = try? c.decodeIfPresent(Double.self, forKey: .lost)
            since = try? c.decodeIfPresent(String.self, forKey: .since)
            until = try? c.decodeIfPresent(String.self, forKey: .until)
            days = try? c.decodeIfPresent(Int.self, forKey: .days)
            measuredDays = try? c.decodeIfPresent(Int.self, forKey: .measuredDays)
            basis = try? c.decodeIfPresent(String.self, forKey: .basis)
        }
    }
    struct UnpricedWin: Codable, Hashable {
        let line: String?
        let module: String?
        let title: String?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            line = try? c.decodeIfPresent(String.self, forKey: .line)
            module = try? c.decodeIfPresent(String.self, forKey: .module)
            title = try? c.decodeIfPresent(String.self, forKey: .title)
        }
        enum CodingKeys: String, CodingKey { case line, module, title }
    }

    /// The improvements alone (the same figure as `total_value_delivered`).
    let monthly: Double?
    /// Improvements less the priced results that got worse.
    let netMonthly: Double?
    let worsened: Worsened?
    let cumulative: Cumulative?
    let unpricedWins: [UnpricedWin]?
    /// I7: what kind of figure each is — `monthly` a "monthly_rate",
    /// `cumulative` a "measured_days_sum" — so no surface prints one under
    /// the other's name. Absent on an older server (read as those two).
    var scope: String? = nil
    var cumulativeScope: String? = nil

    enum CodingKeys: String, CodingKey {
        case monthly, worsened, cumulative, scope
        case netMonthly = "net_monthly"
        case unpricedWins = "unpriced_wins"
        case cumulativeScope = "cumulative_scope"
    }

    /// The band's eyebrow for the figure's period: "PER MONTH" for a monthly
    /// rate (and an older server), "ALL TIME" for an all-time sum — never a
    /// monthly figure under a lifetime label or the reverse.
    static func periodCaption(scope: String?) -> String {
        switch scope?.lowercased() {
        case "all_time_sum", "measured_days_sum": return "ALL TIME"
        default: return "PER MONTH"
        }
    }

    init(monthly: Double?, netMonthly: Double?, worsened: Worsened?,
         cumulative: Cumulative? = nil, unpricedWins: [UnpricedWin]? = nil) {
        self.monthly = monthly
        self.netMonthly = netMonthly
        self.worsened = worsened
        self.cumulative = cumulative
        self.unpricedWins = unpricedWins
    }

    init(from decoder: Decoder) throws {
        // Not even an object: an empty block, never a Home that won't decode.
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        monthly = try? c?.decodeIfPresent(Double.self, forKey: .monthly)
        netMonthly = try? c?.decodeIfPresent(Double.self, forKey: .netMonthly)
        worsened = try? c?.decodeIfPresent(Worsened.self, forKey: .worsened)
        cumulative = try? c?.decodeIfPresent(Cumulative.self, forKey: .cumulative)
        unpricedWins = try? c?.decodeIfPresent([UnpricedWin].self, forKey: .unpricedWins)
        scope = try? c?.decodeIfPresent(String.self, forKey: .scope)
        cumulativeScope = try? c?.decodeIfPresent(String.self, forKey: .cumulativeScope)
    }
}

/// What Home's value band and the value chart draw: the NET figure when
/// something measured got worse (labelled net, with what it is made of),
/// otherwise the improvements exactly as before. Pure, so the rule is
/// pinned by tests rather than by one rendered payload.
struct HomeValueHeadline: Equatable {
    /// Dollars per month to draw — may be below zero when net.
    let figure: Int
    let isNet: Bool
    /// "$1,517 improved, less $600 from 1 that got worse" — only when net.
    let breakdown: String?

    static func make(total: Int, value: HomeValueBlock?) -> HomeValueHeadline {
        guard let value, let worse = value.worsened, (worse.count ?? 0) > 0 else {
            return HomeValueHeadline(figure: total, isNet: false, breakdown: nil)
        }
        let improved = value.monthly ?? Double(total)
        guard let net = value.netMonthly ?? worse.monthly.map({ improved - $0 }) else {
            return HomeValueHeadline(figure: total, isNet: false, breakdown: nil)
        }
        return HomeValueHeadline(
            figure: Int(net.rounded()), isNet: true,
            breakdown: RecValueFormat.netBreakdown(improved: improved, worseMonthly: worse.monthly,
                                                   count: worse.count, pricedCount: worse.pricedCount))
    }
}

extension HomeSummary {
    var valueHeadline: HomeValueHeadline { .make(total: totalValueDelivered, value: value) }
}

/// One entry in the active-modules list. `icon` is a small semantic
/// vocabulary the backend controls (e.g. "reviews", "labor") — NOT a
/// literal SF Symbol name; ModuleIcon.swift owns the actual symbol mapping
/// so either side can change independently (a new backend module needs no
/// app update to show its Home tile; a symbol tweak needs no backend
/// redeploy).
struct ModuleSummary: Codable, Identifiable, Hashable {
    let key: String
    let label: String
    let icon: String
    /// "available" or "coming_soon" — models.get_active_modules() on the
    /// backend. A coming_soon module (Waitlist/Bar today) routes to
    /// ComingSoonView instead of a real screen.
    let status: String
    let kpi: ModuleKPI?
    /// Home's pulse-strip chip for this module — the KPI value with a
    /// short label and a semantic tone. Defaulted so the Modules tab's
    /// static coming-soon entries (built with the memberwise init) keep
    /// compiling untouched.
    var pulse: ModulePulse? = nil

    var id: String { key }
    var isAvailable: Bool { status == "available" }
}

struct ModuleKPI: Codable, Hashable {
    let value: String
    let sublabel: String
}

/// "12/14 · replies · 86%" with a breathing dot — `tone` is "bad" (a named
/// threshold crossed: labor LABOR_OVER_TARGET_PTS over target, an urgent
/// review owed a reply), "warn", "good" or nil (ember), decided server-side
/// from the same thresholds the alerts use (mobile_api.py's _home_pulse).
struct ModulePulse: Codable, Hashable {
    let value: String
    let label: String
    let tone: String?
}

/// `module` names which module a tap should navigate into — a key into the
/// Modules registry, not a literal tab name (the app has no per-module tabs
/// anymore).
struct NeedsAttentionItem: Codable, Identifiable {
    let type: String
    let module: String
    let title: String
    let detail: String
    /// The action deck's buttons — primary label, optional secondary link,
    /// and what the primary does: "publish_replies" (one-tap bulk publish
    /// via /mobile/api/reviews/approve-all) or "open_module" (navigate).
    /// All optional so an older cached summary still decodes.
    let cta: String?
    let secondary: String?
    let action: String?
    /// The key an answer is recorded against (rec_ledger), whether the item
    /// may be hidden at all (a critical one may not), and how often it has
    /// been hidden before — the second hide asks why.
    let recKey: String?
    let dismissable: Bool?
    let timesHidden: Int?
    /// How many replies a publish tap sends — the number on its label (the
    /// last 30 days' drafts), never 25 including imported history.
    let count: Int?
    /// What the item rests on, in one line (K4) — shown inline under the
    /// detail on the card, never hover- or press-only.
    let evidence: String?
    /// K1 — how sure Cavnar is, with "Why?". Absent on an older server.
    let confidence: TrustConfidence?
    /// What the item's dollar figure covers (dollars_basis, B4 H7), when the
    /// server sends it.
    var dollarsBasis: String? = nil
    /// Where the tap lands (nav.py): the filter, section or item the card is
    /// about — "reviews?filter=urgent", "labor/overtime" — rather than the
    /// module's top (friction audit #3). Nil from an older server.
    var nav: String? = nil

    var id: String { type }

    /// The evidence line with the dollar scope after it.
    var evidenceLine: String? {
        switch (evidence, dollarsBasis) {
        case let (e?, b?): return e + " \u{00B7} " + b
        case let (e?, nil): return e
        case let (nil, b?): return b.prefix(1).uppercased() + b.dropFirst()
        default: return nil
        }
    }
    var isPublishAction: Bool { action == "publish_replies" }

    enum CodingKeys: String, CodingKey {
        case type, module, title, detail, cta, secondary, action, dismissable, count, evidence, confidence
        case recKey = "rec_key"
        case timesHidden = "times_hidden"
        case dollarsBasis = "dollars_basis"
        case nav
    }
}

extension NeedsAttentionItem {
    /// The four the card cannot draw without are required; every other
    /// field is read leniently (an odd value is nil, never a Home that
    /// fails to decode).
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        module = try c.decode(String.self, forKey: .module)
        title = try c.decode(String.self, forKey: .title)
        detail = try c.decode(String.self, forKey: .detail)
        cta = try? c.decodeIfPresent(String.self, forKey: .cta)
        secondary = try? c.decodeIfPresent(String.self, forKey: .secondary)
        action = try? c.decodeIfPresent(String.self, forKey: .action)
        recKey = try? c.decodeIfPresent(String.self, forKey: .recKey)
        dismissable = try? c.decodeIfPresent(Bool.self, forKey: .dismissable)
        timesHidden = try? c.decodeIfPresent(Int.self, forKey: .timesHidden)
        count = try? c.decodeIfPresent(Int.self, forKey: .count)
        let ev = (try? c.decodeIfPresent(String.self, forKey: .evidence)) ?? nil
        evidence = (ev?.isEmpty ?? true) ? nil : ev
        confidence = try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence)
        dollarsBasis = RecDollarCalibration.basis((try? c.decodeIfPresent(String.self, forKey: .dollarsBasis)) ?? nil)
        let navRaw = (try? c.decodeIfPresent(String.self, forKey: .nav)) ?? nil
        nav = (navRaw?.isEmpty ?? true) ? nil : navRaw
    }
}
