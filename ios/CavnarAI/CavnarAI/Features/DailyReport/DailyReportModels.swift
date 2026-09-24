import Foundation

// The nightly Daily Sales Report (dsr/ on the server), as the mobile routes
// serve it (strategy_routes.py, "the nightly DSR"):
//
//   GET  /mobile/api/dsr                    DSRListResponse
//   GET  /mobile/api/dsr/<date>[?version=]  DSRReport (access.render)
//   GET  /mobile/api/dsr/<date>/status      DSRStatusResponse (access.checklist)
//   POST /mobile/api/dsr/close              DSRCloseResponse
//   GET  /mobile/api/dsr/week?date=         DSRWeekResponse (rollup.week)
//   GET  /mobile/api/dsr/period?date=       (not read by the app yet; same DSRGrid shape)
//
// One stored snapshot, two views: the server has already removed what this
// login may not read (dsr/access.py) — a whole block, an owner-only line
// (budget*, prime_cost*, source_checks), comps/voids/refunds, and any
// narrative line citing one. So the app renders only what the payload has
// and never re-derives a permission. Every metric may be null: a null is a
// dash on screen, never a zero.

// MARK: - A JSON value

/// Any JSON value. A block's `detail` is a different shape per block and
/// grows as collectors do, so it is decoded loosely and read through the
/// typed accessors further down; a detail key this build doesn't know is
/// simply never shown, rather than failing the whole report.
enum JSONValue: Decodable, Hashable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .null }
    }

    subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }

    var string: String? {
        if case .string(let s) = self { return s.isEmpty ? nil : s }
        return nil
    }
    var double: Double? {
        switch self {
        case .number(let n): return n
        case .string(let s): return Double(s)
        default: return nil
        }
    }
    var int: Int? { double.map { Int($0.rounded()) } }
    var bool: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }
    var array: [JSONValue] {
        if case .array(let a) = self { return a }
        return []
    }
    var object: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }
    var isNull: Bool { self == .null }
}

// MARK: - The list

struct DSRListResponse: Decodable {
    let ok: Bool
    let view: String?
    let reports: [DSRSummary]
}

/// One night in the list (access.summary).
struct DSRSummary: Decodable, Hashable, Identifiable {
    let businessDate: String
    let label: String?
    let version: Int?
    let status: String?
    let provisional: Bool
    let missing: [String]
    let finalizedAt: String?

    var id: String { businessDate + "#" + String(version ?? 0) }
    var displayDate: String { label ?? CavnarDate.mdy(businessDate) }
    var phase: DSRPhase { DSRPhase(status: status, provisional: provisional) }

    enum CodingKeys: String, CodingKey {
        case label, version, status, provisional, missing
        case businessDate = "business_date"
        case finalizedAt = "finalized_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        businessDate = try c.decode(String.self, forKey: .businessDate)
        label = try c.decodeIfPresent(String.self, forKey: .label)
        version = try c.decodeIfPresent(Int.self, forKey: .version)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        provisional = (try c.decodeIfPresent(Bool.self, forKey: .provisional)) ?? false
        missing = (try c.decodeIfPresent([String].self, forKey: .missing)) ?? []
        finalizedAt = try c.decodeIfPresent(String.self, forKey: .finalizedAt)
    }
}

// MARK: - Where a night stands

/// The report's state as the owner reads it — Final, Provisional (some data
/// still syncing), Running (the pipeline is still going) or Couldn't finish.
enum DSRPhase: Equatable {
    case running, final, provisional, failed, notStarted

    /// The pipeline's stages (dsr.pipeline / access.STAGE_LABELS) that are
    /// still moving; everything in dsr.TERMINAL_STAGES has stopped.
    static let runningStatuses: Set<String> = ["scheduled", "awaiting_close", "collecting", "writing"]

    init(status: String?, provisional: Bool = false) {
        switch status {
        case "final": self = provisional ? .provisional : .final
        case "provisional": self = .provisional
        case "failed": self = .failed
        case .some(let s) where Self.runningStatuses.contains(s): self = .running
        case .some: self = .running
        case .none: self = .notStarted
        }
    }

    var label: String {
        switch self {
        case .running: return "Running"
        case .final: return "Final"
        case .provisional: return "Provisional"
        case .failed: return "Couldn\u{2019}t finish"
        case .notStarted: return "Not started"
        }
    }

    var isTerminal: Bool { self == .final || self == .provisional || self == .failed }
}

// MARK: - The report

struct DSRFiscal: Decodable, Hashable {
    let weekStart: String?
    let weekEnd: String?
    let fiscalYear: Int?
    let period: Int?
    let week: Int?
    let label: String?

    enum CodingKeys: String, CodingKey {
        case period, week, label
        case weekStart = "week_start"
        case weekEnd = "week_end"
        case fiscalYear = "fiscal_year"
    }
}

struct DSRVersion: Decodable, Hashable, Identifiable {
    let version: Int
    let status: String?
    let trigger: String?
    let provisional: Bool?
    let createdAt: String?
    let finalizedAt: String?

    var id: Int { version }
    var phase: DSRPhase { DSRPhase(status: status, provisional: provisional ?? false) }

    enum CodingKeys: String, CodingKey {
        case version, status, trigger, provisional
        case createdAt = "created_at"
        case finalizedAt = "finalized_at"
    }
}

struct DSRReport: Decodable {
    let view: String?
    let businessDate: String
    let label: String?
    let fiscal: DSRFiscal?
    let version: Int?
    let status: String?
    let provisional: Bool
    let trigger: String?
    let finalizedAt: String?
    let facts: DSRFacts
    let narrative: DSRNarrative?
    let checklist: DSRChecklist?
    let versions: [DSRVersion]

    enum CodingKeys: String, CodingKey {
        case view, label, fiscal, version, status, provisional, trigger, facts, narrative, checklist, versions
        case businessDate = "business_date"
        case finalizedAt = "finalized_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        view = try c.decodeIfPresent(String.self, forKey: .view)
        businessDate = try c.decode(String.self, forKey: .businessDate)
        label = try c.decodeIfPresent(String.self, forKey: .label)
        fiscal = try? c.decodeIfPresent(DSRFiscal.self, forKey: .fiscal)
        version = try c.decodeIfPresent(Int.self, forKey: .version)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        provisional = (try c.decodeIfPresent(Bool.self, forKey: .provisional)) ?? false
        trigger = try c.decodeIfPresent(String.self, forKey: .trigger)
        finalizedAt = try c.decodeIfPresent(String.self, forKey: .finalizedAt)
        facts = (try c.decodeIfPresent(DSRFacts.self, forKey: .facts)) ?? DSRFacts()
        // A narrative that failed or hasn't been written is null — the facts
        // still stand on their own.
        narrative = try? c.decodeIfPresent(DSRNarrative.self, forKey: .narrative)
        checklist = try? c.decodeIfPresent(DSRChecklist.self, forKey: .checklist)
        versions = (try? c.decodeIfPresent([DSRVersion].self, forKey: .versions)) ?? []
    }

    var isOwnerView: Bool { view == "owner" }
    var displayDate: String { label ?? CavnarDate.mdy(businessDate) }
    var phase: DSRPhase { DSRPhase(status: status, provisional: provisional) }

    /// The blocks this login may read, in report order — a withheld block
    /// isn't in the payload at all and so isn't here either.
    var orderedBlocks: [(name: String, block: DSRBlock)] {
        DSRBlock.order.compactMap { name in facts.blocks[name].map { (name, $0) } }
    }
}

struct DSRFacts: Decodable {
    var blocks: [String: DSRBlock] = [:]
    var missing: [String] = []
    var withheld: [String] = []
    var fiscal: DSRFiscal?

    init() {}

    enum CodingKeys: String, CodingKey { case blocks, missing, withheld, fiscal }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // Decoded one block at a time: a block this build can't read must
        // not take the other six down with it.
        if let raw = try? c.decodeIfPresent([String: JSONValue].self, forKey: .blocks) {
            var out: [String: DSRBlock] = [:]
            for (name, value) in raw where !value.isNull {
                out[name] = DSRBlock(json: value)
            }
            blocks = out
        }
        missing = (try? c.decodeIfPresent([String].self, forKey: .missing)) ?? []
        withheld = (try? c.decodeIfPresent([String].self, forKey: .withheld)) ?? []
        fiscal = try? c.decodeIfPresent(DSRFiscal.self, forKey: .fiscal)
    }
}

/// One block of the night: sales, labor, food, reviews, marketing, intel,
/// or the manager's close-out. `status` is "ready" when it was measured;
/// anything else carries the server's own `reason` sentence.
struct DSRBlock: Hashable {
    static let order = ["sales", "labor", "food", "reviews", "marketing", "intel", "closeout"]
    static let titles: [String: String] = [
        "sales": "Sales", "labor": "Labor", "food": "Food", "reviews": "Reviews",
        "marketing": "Marketing", "intel": "Intel", "closeout": "Manager close-out",
    ]

    let status: String?
    let source: String?
    let reason: String?
    /// Only the keys the payload carried; a key present with a null value
    /// is stored as nil, the same as one that is absent — both read "—".
    let metrics: [String: Double?]
    let detail: JSONValue

    init(json: JSONValue) {
        status = json["status"]?.string
        source = json["source"]?.string
        reason = json["reason"]?.string
        var m: [String: Double?] = [:]
        for (k, v) in json["metrics"]?.object ?? [:] { m[k] = v.double }
        metrics = m
        detail = json["detail"] ?? .null
    }

    var isReady: Bool { status == "ready" }

    /// A measured figure, or nil when it is null, absent, or withheld.
    func metric(_ key: String) -> Double? { metrics[key] ?? nil }

    /// Whether the payload named this figure at all — a withheld line is
    /// removed from the payload, a measured-nothing line is present as null.
    func has(_ key: String) -> Bool { metrics.keys.contains(key) }
}

// MARK: - The narrative

/// One sentence the model wrote, with the facts it cites.
struct DSRLine: Decodable, Hashable, Identifiable {
    let text: String
    let cites: [String]
    var id: String { text }

    enum CodingKeys: String, CodingKey { case text, cites, facts }

    init(text: String, cites: [String] = []) {
        self.text = text
        self.cites = cites
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)
        // dsr.narrative's shape is "cites"; the older "facts" name is read too.
        cites = (try? c.decodeIfPresent([String].self, forKey: .cites))
            ?? (try? c.decodeIfPresent([String].self, forKey: .facts)) ?? []
    }
}

/// One of tomorrow's actions.
struct DSRAction: Decodable, Hashable, Identifiable {
    let text: String
    let why: String?
    let urgency: String?
    let effort: String?
    let kind: String?
    let dollarsMonthly: Double?
    let key: String?
    /// The contract fields the report view adds (strategy_routes
    /// `_dsr_present_view`): the action's rec_ledger key, whether Done /
    /// Not for us / Track apply, and whether the owner already answered it
    /// — the report is a record, so an answered line stays and loses its
    /// controls. All optional: an older server sends only `key`.
    var recKey: String? = nil
    var answered: Bool? = nil
    var answerable: Bool? = nil
    /// K1, built from the facts the action cites (E16) — the shared
    /// confidence line under the action. Absent on an older report.
    var confidence: TrustConfidence? = nil
    /// H13: what the facts the action cites say about "Today" — and, when
    /// they did not carry it, the move the server made ({from, to, why}:
    /// "before_service" → "this_week"). Absent on an older report.
    var urgencyBasis: String? = nil
    var urgencyAdjusted: UrgencyAdjusted? = nil
    /// F6 (rec_learning.attach_dollar_calibration): the dollars corrected by
    /// this restaurant's measured results for the kind — null means show
    /// `dollars_monthly` as it is — how many results, and the note ("adjusted
    /// from 6 measured results"). Absent until the server sends them.
    var dollarsAdjusted: Double? = nil
    var calibrationN: Int? = nil
    var calibrationNote: String? = nil
    var id: String { key ?? text }

    struct UrgencyAdjusted: Decodable, Hashable {
        let from: String?
        let to: String?
        let why: String?
    }

    enum CodingKeys: String, CodingKey {
        case text, why, urgency, effort, kind, key, answered, answerable, confidence
        case dollarsMonthly = "dollars_monthly"
        case recKey = "rec_key"
        case urgencyBasis = "urgency_basis"
        case urgencyAdjusted = "urgency_adjusted"
        case dollarsAdjusted = "dollars_adjusted"
        case calibrationN = "calibration_n"
        case calibrationNote = "calibration_note"
    }

    /// "Moved from Before service to This week — nothing it cites moved 10%
    /// (2 points) from what it is compared with." Nil unless the server
    /// moved it.
    var urgencyAdjustedLine: String? {
        guard let adj = urgencyAdjusted, adj.from != nil || adj.to != nil else { return nil }
        func words(_ s: String?) -> String? {
            guard let s, !s.isEmpty else { return nil }
            let w = s.replacingOccurrences(of: "_", with: " ")
            return w.prefix(1).uppercased() + w.dropFirst()
        }
        var s = "Moved"
        if let f = words(adj.from) { s += " from \(f)" }
        if let t = words(adj.to) { s += " to \(t)" }
        if let why = adj.why, !why.isEmpty { s += " \u{2014} \(why)" }
        return s
    }

    /// The dollars to show and what corrected them — the adjusted figure
    /// and its note when the server calibrated it, else the raw figure.
    var dollarsLine: String? { RecDollarCalibration.line(raw: dollarsMonthly, adjusted: dollarsAdjusted,
                                                          n: calibrationN, note: calibrationNote) }

    /// The key the answer row posts — rec_key, else the action's own key.
    var answerKey: String? {
        let k = recKey ?? key
        return (k?.isEmpty == false) ? k : nil
    }

    /// Done / Not for us / Track under the action: only for a keyed action
    /// the owner has not answered and the server calls answerable (an older
    /// server that sends no flag: any keyed, unanswered action).
    var showsAnswers: Bool {
        answerKey != nil && answered != true && answerable != false
    }

    /// The module the answer is credited to — the block in the key
    /// (`dsr_action:<kind>:<block>[/<entity>]`), in the web's vocabulary
    /// (dashboard.html's DSR MODULE map): sales and the close-out are ops.
    var answerModule: String {
        let parts = (answerKey ?? "").split(separator: ":", omittingEmptySubsequences: false)
        let block = parts.count > 2 ? String(parts[2].split(separator: "/").first ?? "") : ""
        switch block {
        case "labor", "food", "reviews", "marketing", "intel": return block
        default: return "ops"
        }
    }

    /// "This week", "Next schedule", "Tonight" — the server's own words,
    /// made readable.
    var urgencyLabel: String? {
        guard let u = urgency, !u.isEmpty else { return nil }
        let words = u.replacingOccurrences(of: "_", with: " ")
        return words.prefix(1).uppercased() + words.dropFirst()
    }

    var effortLabel: String? {
        guard let e = effort, !e.isEmpty else { return nil }
        return "\(e) effort"
    }
}

/// dsr.narrative's `verification`: lines checked, lines kept, and the lines
/// dropped because a figure didn't trace (a list of {field, text, why} —
/// only its length is shown). H13: estimates the lines cite are counted
/// apart (`estimated` / `estimates`, a count or a list), so "traced to a
/// measured fact" never covers them. Every field lenient.
struct DSRVerification: Decodable, Hashable {
    let checked: Int?
    let kept: Int?
    let dropped: Int?
    let estimated: Int?
    /// H13 (dsr/narrative.py): the kept lines resting on measured facts
    /// only — kept minus estimated, as the server counts it.
    var measured: Int? = nil

    enum CodingKeys: String, CodingKey { case checked, kept, dropped, estimated, estimates, measured }

    init(checked: Int?, kept: Int?, dropped: Int? = nil, estimated: Int? = nil, measured: Int? = nil) {
        self.checked = checked; self.kept = kept; self.dropped = dropped; self.estimated = estimated
        self.measured = measured
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        checked = try? c.decodeIfPresent(Int.self, forKey: .checked)
        kept = try? c.decodeIfPresent(Int.self, forKey: .kept)
        dropped = Self.count(c, .dropped)
        estimated = Self.count(c, .estimated) ?? Self.count(c, .estimates)
        measured = try? c.decodeIfPresent(Int.self, forKey: .measured)
    }

    /// A count sent as a number or as the list it counts.
    private static func count(_ c: KeyedDecodingContainer<CodingKeys>, _ key: CodingKeys) -> Int? {
        if let n = (try? c.decodeIfPresent(Int.self, forKey: key)) ?? nil { return n }
        if let list = (try? c.decodeIfPresent([JSONValue].self, forKey: key)) ?? nil { return list.count }
        return nil
    }

    /// "Every figure above traced to a measured fact · 7 of 8 lines kept ·
    /// 1 dropped because a figure didn't trace · 2 estimates, labelled as
    /// such" — nil when nothing was checked.
    var footer: String? {
        guard let checked, let kept, checked > 0 else { return nil }
        // With the server's own split (H13), "traced to a measured fact"
        // never covers the lines that rest on an estimate.
        if let m = measured, let e = estimated, e > 0 {
            var s = "Every figure above traced to a fact it cites \u{00B7} \(kept) of \(checked) lines kept \u{00B7} "
                + "\(m) measured, \(e) estimated (labelled as such)"
            if let d = dropped, d > 0 { s += " \u{00B7} \(d) dropped because a figure didn\u{2019}t trace" }
            return s
        }
        var s = "Every figure above traced to a measured fact \u{00B7} \(kept) of \(checked) lines kept"
        if let d = dropped, d > 0 { s += " \u{00B7} \(d) dropped because a figure didn\u{2019}t trace" }
        if let e = estimated, e > 0 { s += " \u{00B7} \(e) estimate\(e == 1 ? "" : "s"), labelled as such" }
        return s
    }
}

struct DSRNarrative: Decodable {
    let executiveSummary: DSRLine?
    let wentWell: [DSRLine]
    let needsAttention: [DSRLine]
    let highestPriorityIssue: DSRLine?
    let biggestRisk: DSRLine?
    let biggestWin: DSRLine?
    let biggestStaffingConcern: DSRLine?
    let biggestFinancialOpportunity: DSRLine?
    let largestGuestExperience: DSRLine?
    let actionsTomorrow: [DSRAction]
    let verification: DSRVerification?
    let model: String?

    enum CodingKeys: String, CodingKey {
        case model, verification
        case executiveSummary = "executive_summary"
        case wentWell = "went_well"
        case needsAttention = "needs_attention"
        case highestPriorityIssue = "highest_priority_issue"
        case biggestRisk = "biggest_risk"
        case biggestWin = "biggest_win"
        case biggestStaffingConcern = "biggest_staffing_concern"
        case biggestFinancialOpportunity = "biggest_financial_opportunity"
        case largestGuestExperience = "largest_guest_experience"
        case actionsTomorrow = "actions_tomorrow"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // Every field is optional: the manager payload drops any line that
        // cites a figure it withholds, and an empty object is a valid answer.
        executiveSummary = try? c.decodeIfPresent(DSRLine.self, forKey: .executiveSummary)
        wentWell = (try? c.decodeIfPresent([DSRLine].self, forKey: .wentWell)) ?? []
        needsAttention = (try? c.decodeIfPresent([DSRLine].self, forKey: .needsAttention)) ?? []
        highestPriorityIssue = try? c.decodeIfPresent(DSRLine.self, forKey: .highestPriorityIssue)
        biggestRisk = try? c.decodeIfPresent(DSRLine.self, forKey: .biggestRisk)
        biggestWin = try? c.decodeIfPresent(DSRLine.self, forKey: .biggestWin)
        biggestStaffingConcern = try? c.decodeIfPresent(DSRLine.self, forKey: .biggestStaffingConcern)
        biggestFinancialOpportunity = try? c.decodeIfPresent(DSRLine.self, forKey: .biggestFinancialOpportunity)
        largestGuestExperience = try? c.decodeIfPresent(DSRLine.self, forKey: .largestGuestExperience)
        actionsTomorrow = (try? c.decodeIfPresent([DSRAction].self, forKey: .actionsTomorrow)) ?? []
        verification = try? c.decodeIfPresent(DSRVerification.self, forKey: .verification)
        model = try? c.decodeIfPresent(String.self, forKey: .model)
    }

    /// The "biggest" call-outs, labelled as the report labels them, in the
    /// order they are read; only the ones the payload has.
    var callouts: [(label: String, line: DSRLine)] {
        let all: [(String, DSRLine?)] = [
            ("Highest priority", highestPriorityIssue), ("Biggest risk", biggestRisk),
            ("Biggest win", biggestWin), ("Staffing", biggestStaffingConcern),
            ("Money on the table", biggestFinancialOpportunity), ("Guest experience", largestGuestExperience),
        ]
        return all.compactMap { label, line in line.map { (label, $0) } }
    }

    var isEmpty: Bool {
        executiveSummary == nil && wentWell.isEmpty && needsAttention.isEmpty
            && callouts.isEmpty && actionsTomorrow.isEmpty
    }
}

// MARK: - Progress

struct DSRStage: Decodable, Hashable, Identifiable {
    let key: String
    let label: String
    let at: String?
    let atLocal: String?
    let done: Bool
    let current: Bool
    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, label, at, done, current
        case atLocal = "at_local"
    }
}

struct DSRChecklistBlock: Decodable, Hashable, Identifiable {
    let name: String
    let label: String
    let status: String?
    let reason: String?
    let atLocal: String?
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, label, status, reason
        case atLocal = "at_local"
    }
}

struct DSRNarrativeState: Decodable, Hashable {
    let status: String?
    let reason: String?
}

/// access.checklist — the stage list the progressive screen ticks through.
struct DSRChecklist: Decodable {
    let businessDate: String?
    let label: String?
    let version: Int?
    let status: String?
    let statusLabel: String?
    let provisional: Bool
    let stages: [DSRStage]
    let blocks: [DSRChecklistBlock]
    let narrative: DSRNarrativeState?
    let closedBy: String?
    let nextAttemptAt: String?
    let missing: [String]

    enum CodingKeys: String, CodingKey {
        case label, version, status, provisional, stages, blocks, narrative, missing
        case businessDate = "business_date"
        case statusLabel = "status_label"
        case closedBy = "closed_by"
        case nextAttemptAt = "next_attempt_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        businessDate = try c.decodeIfPresent(String.self, forKey: .businessDate)
        label = try c.decodeIfPresent(String.self, forKey: .label)
        version = try c.decodeIfPresent(Int.self, forKey: .version)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        statusLabel = try c.decodeIfPresent(String.self, forKey: .statusLabel)
        provisional = (try c.decodeIfPresent(Bool.self, forKey: .provisional)) ?? false
        stages = (try? c.decodeIfPresent([DSRStage].self, forKey: .stages)) ?? []
        blocks = (try? c.decodeIfPresent([DSRChecklistBlock].self, forKey: .blocks)) ?? []
        narrative = try? c.decodeIfPresent(DSRNarrativeState.self, forKey: .narrative)
        closedBy = try c.decodeIfPresent(String.self, forKey: .closedBy)
        nextAttemptAt = try c.decodeIfPresent(String.self, forKey: .nextAttemptAt)
        missing = (try? c.decodeIfPresent([String].self, forKey: .missing)) ?? []
    }

    var phase: DSRPhase { DSRPhase(status: status, provisional: provisional) }
}

/// GET /dsr/<date>/status: the checklist, or `exists: false` when nothing
/// has started for that night — an answer, not an error.
struct DSRStatusResponse: Decodable {
    let ok: Bool
    let view: String?
    let exists: Bool
    let checklist: DSRChecklist?

    enum CodingKeys: String, CodingKey { case ok, view, exists }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try c.decodeIfPresent(Bool.self, forKey: .ok)) ?? false
        view = try c.decodeIfPresent(String.self, forKey: .view)
        exists = (try c.decodeIfPresent(Bool.self, forKey: .exists)) ?? false
        checklist = exists ? try? DSRChecklist(from: decoder) : nil
    }
}

/// POST /dsr/close — the night the server is running, and whether it started.
struct DSRCloseResponse: Decodable {
    let ok: Bool
    let businessDate: String?
    let label: String?
    let started: Bool?
    let status: String?
    let version: Int?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, label, started, status, version, error
        case businessDate = "business_date"
    }
}

// MARK: - The weekly grid

struct DSRWeekResponse: Decodable {
    let ok: Bool
    let view: String?
    let week: DSRGrid
}

/// A week (one row per day) or a period (one row per week), from dsr.rollup.
struct DSRGrid: Decodable {
    let kind: String?
    let start: String
    let end: String
    let label: String?
    let fiscal: DSRFiscal?
    let categories: [String]
    let days: [DSRGridDay]
    let weeks: [DSRGridWeek]
    let totals: DSRGridTotals?
    let periodToDate: DSRGridTotals?
    /// ["budget"] for a manager — the budget columns aren't in the payload.
    let withheld: [String]

    enum CodingKeys: String, CodingKey {
        case kind, start, end, label, fiscal, categories, days, weeks, totals, withheld
        case periodToDate = "period_to_date"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decodeIfPresent(String.self, forKey: .kind)
        start = try c.decode(String.self, forKey: .start)
        end = try c.decode(String.self, forKey: .end)
        label = try c.decodeIfPresent(String.self, forKey: .label)
        fiscal = try? c.decodeIfPresent(DSRFiscal.self, forKey: .fiscal)
        categories = (try c.decodeIfPresent([String].self, forKey: .categories)) ?? []
        days = (try c.decodeIfPresent([DSRGridDay].self, forKey: .days)) ?? []
        weeks = (try c.decodeIfPresent([DSRGridWeek].self, forKey: .weeks)) ?? []
        totals = try c.decodeIfPresent(DSRGridTotals.self, forKey: .totals)
        periodToDate = try c.decodeIfPresent(DSRGridTotals.self, forKey: .periodToDate)
        withheld = (try c.decodeIfPresent([String].self, forKey: .withheld)) ?? []
    }

    var showsBudget: Bool { !withheld.contains("budget") }
}

struct DSRGridDay: Decodable, Identifiable {
    let date: String
    let weekday: String?
    let label: String?
    let status: String?
    let provisional: Bool?
    let version: Int?
    let cats: [String: Double?]
    let gross: Double?
    let net: Double?
    let budgetGross: Double?
    let budgetNet: Double?
    let lastYearNet: Double?
    let lastYearSource: String?
    let laborCost: Double?
    let laborPct: Double?
    let weather: String?
    let event: String?
    let influence: String?
    let vsBudgetNetPct: Double?
    let vsLastYearNetPct: Double?

    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, weekday, label, status, provisional, version, cats, gross, net, weather, event, influence
        case budgetGross = "budget_gross"
        case budgetNet = "budget_net"
        case lastYearNet = "last_year_net"
        case lastYearSource = "last_year_source"
        case laborCost = "labor_cost"
        case laborPct = "labor_pct"
        case vsBudgetNetPct = "vs_budget_net_pct"
        case vsLastYearNetPct = "vs_last_year_net_pct"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = try c.decode(String.self, forKey: .date)
        weekday = try c.decodeIfPresent(String.self, forKey: .weekday)
        label = try c.decodeIfPresent(String.self, forKey: .label)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        provisional = try c.decodeIfPresent(Bool.self, forKey: .provisional)
        version = try c.decodeIfPresent(Int.self, forKey: .version)
        cats = (try c.decodeIfPresent([String: Double?].self, forKey: .cats)) ?? [:]
        gross = try c.decodeIfPresent(Double.self, forKey: .gross)
        net = try c.decodeIfPresent(Double.self, forKey: .net)
        budgetGross = try c.decodeIfPresent(Double.self, forKey: .budgetGross)
        budgetNet = try c.decodeIfPresent(Double.self, forKey: .budgetNet)
        lastYearNet = try c.decodeIfPresent(Double.self, forKey: .lastYearNet)
        lastYearSource = try c.decodeIfPresent(String.self, forKey: .lastYearSource)
        laborCost = try c.decodeIfPresent(Double.self, forKey: .laborCost)
        laborPct = try c.decodeIfPresent(Double.self, forKey: .laborPct)
        weather = try c.decodeIfPresent(String.self, forKey: .weather)
        event = try c.decodeIfPresent(String.self, forKey: .event)
        influence = try c.decodeIfPresent(String.self, forKey: .influence)
        vsBudgetNetPct = try c.decodeIfPresent(Double.self, forKey: .vsBudgetNetPct)
        vsLastYearNetPct = try c.decodeIfPresent(Double.self, forKey: .vsLastYearNetPct)
    }

    /// "Wed 9/16/26" — the server's label, or built from the date.
    var displayLabel: String { label ?? "\(weekday ?? "") \(CavnarDate.mdy(date))".trimmingCharacters(in: .whitespaces) }
    func category(_ name: String) -> Double? { cats[name] ?? nil }

    /// The day's notes, joined: weather, event, and the manager's
    /// "why the day went how it did".
    var notes: String? {
        let parts = [weather, event, influence].compactMap { $0 }.filter { !$0.isEmpty }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

struct DSRGridWeek: Decodable, Identifiable {
    let start: String
    let end: String
    let label: String?
    let totals: DSRGridTotals?
    var id: String { start }
}

/// A totals row. Sums only the days that were measured (`daysMeasured`);
/// a comparison is only between days with both sides.
struct DSRGridTotals: Decodable {
    let gross: Double?
    let net: Double?
    let budgetGross: Double?
    let budgetNet: Double?
    let lastYearNet: Double?
    let laborCost: Double?
    let laborPct: Double?
    let cats: [String: Double?]
    let daysMeasured: Int?
    let vsBudgetNetPct: Double?
    let vsLastYearNetPct: Double?
    let start: String?
    let end: String?

    enum CodingKeys: String, CodingKey {
        case gross, net, cats, start, end
        case budgetGross = "budget_gross"
        case budgetNet = "budget_net"
        case lastYearNet = "last_year_net"
        case laborCost = "labor_cost"
        case laborPct = "labor_pct"
        case daysMeasured = "days_measured"
        case vsBudgetNetPct = "vs_budget_net_pct"
        case vsLastYearNetPct = "vs_last_year_net_pct"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        gross = try c.decodeIfPresent(Double.self, forKey: .gross)
        net = try c.decodeIfPresent(Double.self, forKey: .net)
        budgetGross = try c.decodeIfPresent(Double.self, forKey: .budgetGross)
        budgetNet = try c.decodeIfPresent(Double.self, forKey: .budgetNet)
        lastYearNet = try c.decodeIfPresent(Double.self, forKey: .lastYearNet)
        laborCost = try c.decodeIfPresent(Double.self, forKey: .laborCost)
        laborPct = try c.decodeIfPresent(Double.self, forKey: .laborPct)
        cats = (try c.decodeIfPresent([String: Double?].self, forKey: .cats)) ?? [:]
        daysMeasured = try c.decodeIfPresent(Int.self, forKey: .daysMeasured)
        vsBudgetNetPct = try c.decodeIfPresent(Double.self, forKey: .vsBudgetNetPct)
        vsLastYearNetPct = try c.decodeIfPresent(Double.self, forKey: .vsLastYearNetPct)
        start = try c.decodeIfPresent(String.self, forKey: .start)
        end = try c.decodeIfPresent(String.self, forKey: .end)
    }

    func category(_ name: String) -> Double? { cats[name] ?? nil }
}
