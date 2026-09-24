import Foundation
import Observation

// MARK: - Roster

/// What the owner has said about one person that the generator obeys:
/// on the roster or not, full or part time, an hours band, whether they
/// are a minor, and which part of each day they can work.
struct RosterSettings: Codable, Equatable {
    var active: Bool?
    var employmentType: String?
    var minHours: Double?
    var maxHours: Double?
    var daypartAvailability: [String: String]?
    var isMinor: Bool?
    // The earliest start and latest end they can work, per weekday.
    var timeWindows: [String: TimeWindow]?
    // What they hold — "food handler", "alcohol", a keyholder.
    var certifications: [String]?
    // What the person said in the portal: the dayparts they prefer and
    // the hours they want. Read-only here.
    var preferredDayparts: [String]?
    var desiredHours: Double?
    // The owner's word that this person knows the job — counted by
    // Experience balance without waiting for twenty shifts on file.
    var experienced: Bool?
    // Which minor rule table applies: "14-15" (the federal school-day
    // limits) or "16-17"; nil for an adult or a minor with no band yet
    // (NS5 H4). Optional so an older backend decodes.
    var minorAgeBand: String?

    enum CodingKeys: String, CodingKey {
        case active, certifications, experienced
        case minorAgeBand = "minor_age_band"
        case employmentType = "employment_type"
        case minHours = "min_hours"
        case maxHours = "max_hours"
        case daypartAvailability = "daypart_availability"
        case isMinor = "is_minor"
        case timeWindows = "time_windows"
        case preferredDayparts = "preferred_dayparts"
        case desiredHours = "desired_hours"
    }
}

/// One weekday's window: "10:00am" to "9:00pm". Either end may be blank.
struct TimeWindow: Codable, Equatable {
    var earliest: String?
    var latest: String?

    var isEmpty: Bool { (earliest ?? "").isEmpty && (latest ?? "").isEmpty }
}

/// How often somebody has not turned up, measured from the shifts they
/// were on. Nil when there is nothing to measure it from.
struct RosterReliability: Codable, Equatable {
    /// SMOOTHED toward the restaurant's own base rate (I9 — two misses in
    /// six shifts no longer read as a flat 33%).
    let noShowRate: Double?
    let shortRate: Double?
    let shifts: Int?
    /// I9 (staff_settings.reliability): the raw count and rate beside the
    /// smoothed one, the base rate, and the engine's own red line
    /// (shift_quality.UNRELIABLE_RATE, 20%) with whether this person is past
    /// it — the same line the scheduler treats them by. Absent on an older
    /// server.
    var noShows: Int? = nil
    var rawNoShowRate: Double? = nil
    var baseRate: Double? = nil
    var noShowThreshold: Double? = nil
    var unreliable: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case shifts, unreliable
        case noShowRate = "no_show_rate"
        case shortRate = "short_rate"
        case noShows = "no_shows"
        case rawNoShowRate = "raw_no_show_rate"
        case baseRate = "base_rate"
        case noShowThreshold = "no_show_threshold"
    }

    private static func pct(_ rate: Double) -> Int { Int((rate > 1 ? rate : rate * 100).rounded()) }

    /// "4% no-show (1 of 12 shifts)" — a rate is stored as a fraction (0.04)
    /// or, on some rows, as a percentage already; anything above 1 is taken
    /// as one. The raw count rides beside the smoothed rate when sent.
    var noShowLabel: String? {
        guard let rate = noShowRate else { return nil }
        var s = "\(Self.pct(rate))% no-show"
        if let n = noShows, let total = shifts, total > 0 { s += " (\(n) of \(total) shifts)" }
        if isUnreliable { s += " \u{2014} past the \(Self.pct(noShowThreshold ?? 0.2))% line the scheduler uses" }
        return s
    }

    /// Past the engine's own line: the server's `unreliable`, else the
    /// smoothed rate against `no_show_threshold`; false for an older server.
    var isUnreliable: Bool {
        if let unreliable { return unreliable }
        guard let rate = noShowRate, let line = noShowThreshold else { return false }
        return rate >= line
    }
}

struct RosterMember: Codable, Identifiable, Equatable {
    let name: String
    let role: String?
    let shifts: Int?
    let lastWorked: String?
    let isManual: Bool?
    var active: Bool?
    var settings: RosterSettings?
    let score: Int?
    let canClose: Bool?
    let reliability: RosterReliability?

    var id: String { name }
    var isActive: Bool { active ?? settings?.active ?? true }

    enum CodingKeys: String, CodingKey {
        case name, role, shifts, active, settings, score, reliability
        case lastWorked = "last_worked"
        case isManual = "is_manual"
        case canClose = "can_close"
    }
}

/// Two people the owner wants together, or apart.
struct StaffPair: Codable, Identifiable, Equatable {
    let id: Int
    let a: String
    let b: String
    let kind: String
    let note: String?

    var isPrefer: Bool { kind == "prefer" }
}

struct RosterChoices: Codable, Equatable {
    let employmentType: [String]?
    let daypart: [String]?
    let days: [String]?
    let certifications: [String]?

    enum CodingKeys: String, CodingKey {
        case daypart, days, certifications
        case employmentType = "employment_type"
    }
}

/// Two people the record says work well together. Never applied on its
/// own — "Add" creates the pair, "Ignore" hides the suggestion.
struct SuggestedPair: Codable, Identifiable, Equatable {
    let a: String
    let b: String
    let kind: String?
    let shared: Int?
    let cleanRate: Double?
    let evidence: String?
    let recKey: String?

    var id: String { "\(a)|\(b)" }

    enum CodingKeys: String, CodingKey {
        case a, b, kind, shared, evidence
        case cleanRate = "clean_rate"
        case recKey = "rec_key"
    }
}

/// What the draft has learned from the manager's edits — one sentence,
/// and whether it is still in use.
struct LearnedPattern: Codable, Identifiable, Equatable {
    let kind: String?
    let employee: String?
    let day: String?
    let daypart: String?
    let times: Int?
    let text: String?
    let key: String
    var active: Bool?
    var dismissed: Bool?

    var id: String { key }
}

// MARK: - Rules

/// Per-role headcount floors: at least this many on a morning, this many
/// on a night, with optional per-day overrides.
struct DayFloor: Codable, Equatable {
    var morning: Int?
    var night: Int?
}

struct RoleFloor: Codable, Equatable {
    var morning: Int?
    var night: Int?
    var days: [String: DayFloor]?
}

/// A jurisdiction's rule pack: what it set, and the notes an owner
/// should read with counsel.
struct CompliancePack: Codable, Equatable {
    let code: String?
    let label: String?
    let applied: [String: LooseValue]?
    let notes: [String]?
}

struct CodeLabel: Codable, Identifiable, Equatable {
    let code: String
    let label: String
    let live: Bool?
    var id: String { code }
}

// MARK: - Demand signals

/// A dated reason to expect more (or fewer) covers — a private party, a
/// block of reservations, a street closure.
struct DemandSignal: Codable, Identifiable, Equatable {
    let id: Int
    let date: String
    let kind: String
    let label: String?
    let covers: Int?
    let liftPct: Double?
    let source: String?

    enum CodingKeys: String, CodingKey {
        case id, date, kind, label, covers, source
        case liftPct = "lift_pct"
    }

    var kindLabel: String { kind == "reservations" ? "Reservations" : "Event" }
}

// MARK: - Shift requests

/// A shift somebody has asked to give up (`pending`) or that nobody has
/// picked up yet (`open`).
struct ShiftRequest: Codable, Identifiable, Equatable {
    let id: Int
    let employeeName: String?
    let date: String
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?
    let reason: String?
    let status: String
    let createdAt: String?
    // "drop" (hand the shift back) or "swap" (trade with a colleague's).
    let kind: String?
    let targetName: String?
    let targetDate: String?
    let targetStart: String?
    let targetEnd: String?

    enum CodingKeys: String, CodingKey {
        case id, date, role, reason, status, kind
        case employeeName = "employee_name"
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case createdAt = "created_at"
        case targetName = "target_name"
        case targetDate = "target_date"
        case targetStart = "target_start"
        case targetEnd = "target_end"
    }

    var isSwap: Bool { (kind ?? "drop") == "swap" }
    var kindLabel: String { isSwap ? "Swap" : "Drop" }

    /// "Ana ↔ Bob: Mon 4:00pm for Wed 4:00pm"
    var swapLabel: String? {
        guard isSwap else { return nil }
        let a = employeeName ?? "Somebody"
        let b = targetName ?? "a colleague"
        let mine = [Self.shortDay(date), shiftStart].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
        let theirs = [targetDate.map(Self.shortDay), targetStart].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
        return "\(a) ↔ \(b): \(mine) for \(theirs)"
    }

    /// "Mon" from an ISO date; the M/D/YY date when it cannot be read.
    static func shortDay(_ iso: String) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        guard let d = f.date(from: String(iso.prefix(10))) else { return CavnarDate.mdy(iso) }
        let out = DateFormatter()
        out.dateFormat = "EEE"
        out.locale = Locale(identifier: "en_US_POSIX")
        return out.string(from: d)
    }

    /// "9/21/26 · 4:00pm–close · Server"
    var whenLabel: String {
        var parts = [CavnarDate.mdy(date)]
        if let s = shiftStart, !s.isEmpty {
            parts.append((shiftEnd ?? "").isEmpty ? s : "\(s)–\(shiftEnd ?? "")")
        }
        if let role, !role.isEmpty { parts.append(role) }
        return parts.joined(separator: " · ")
    }
}


// MARK: - Intel

/// One weekday × daypart from the record: how the shift went, on
/// average, over the weeks it has data for.
struct IntelOutcome: Codable, Equatable {
    let weeks: Int?
    let avgHours: Double?
    let avgSales: Double?
    let splh: Double?
    /// Null when no night in the window was watched at all (F8) — which is
    /// "not watched", never "no issues".
    let issues: Int?
    let troubled: Bool?
    let rating: Double?
    /// F8: the server's words for the coverage record — "2 issues", "no
    /// issues on 3 watched nights", "not watched — coverage wasn't checked
    /// on these nights" — and how many nights were a reading at all.
    var issuesLabel: String? = nil
    var watched: Int? = nil

    enum CodingKeys: String, CodingKey {
        case weeks, splh, issues, troubled, rating, watched
        case avgHours = "avg_hours"
        case avgSales = "avg_sales"
        case issuesLabel = "issues_label"
    }

    /// What the detail line says about issues: the server's label when sent
    /// (it tells "no issues on 3 watched nights" from "not watched"); an
    /// older server's count only when above zero; nothing otherwise — a nil
    /// or zero count is never printed as "no issues".
    var issuesText: String? {
        if let label = issuesLabel?.trimmingCharacters(in: .whitespaces), !label.isEmpty { return label }
        if let i = issues, i > 0 { return "\(i) \(i == 1 ? "issue" : "issues")" }
        return nil
    }
}

/// Weekend, closing and holiday shifts somebody has carried lately.
struct IntelLedgerEntry: Codable, Equatable {
    let weekend: Int?
    let closing: Int?
    let holiday: Int?
    let shifts: Int?
    let weeks: Int?
}

/// What somebody keeps dropping and picking up.
struct IntelBehaviour: Codable, Equatable {
    let avoids: [String]?
    let prefers: [String]?
    let drops: Int?
    let claims: Int?
}

struct IntelSplh: Codable, Equatable {
    let sales: Double?
    let hours: Double?
    let splh: Double?
}

struct IntelRevenue: Codable, Equatable {
    let value: Double?
    let source: String?
    let weeks: Int?
    /// K8, when the server puts it on the projection itself.
    var demandAccuracy: DemandAccuracy? = nil

    enum CodingKeys: String, CodingKey {
        case value, source, weeks
        case demandAccuracy = "demand_accuracy"
    }
}

/// One published week: how much of the generated draft went out as it was.
struct AcceptanceWeek: Codable, Identifiable, Equatable {
    let historyId: Int?
    let weekStart: String?
    let changes: Int?
    let unchangedShare: Double?

    var id: String { "\(historyId ?? 0)-\(weekStart ?? "")" }

    enum CodingKeys: String, CodingKey {
        case changes
        case historyId = "history_id"
        case weekStart = "week_start"
        case unchangedShare = "unchanged_share"
    }
}

struct AcceptanceTrend: Codable, Equatable {
    let direction: String?
    let older: Double?
    let newer: Double?
}

/// How much of each draft survives to the published week. Weeks arrive
/// newest first.
struct DraftAcceptance: Codable, Equatable {
    let available: Bool?
    let weeks: [AcceptanceWeek]?
    let trend: AcceptanceTrend?
    let meanUnchangedShare: Double?
    let meanChanges: Double?

    enum CodingKeys: String, CodingKey {
        case available, weeks, trend
        case meanUnchangedShare = "mean_unchanged_share"
        case meanChanges = "mean_changes"
    }

    /// Oldest first, only weeks with a measured share — the chart's order.
    var chartWeeks: [AcceptanceWeek] { (weeks ?? []).reversed().filter { $0.unchangedShare != nil } }

    /// One sentence on the direction, from the payload's own figures.
    var trendLine: String? {
        guard let t = trend, let older = t.older, let newer = t.newer else { return nil }
        let o = Int((older * 100).rounded()), n = Int((newer * 100).rounded())
        switch t.direction {
        case "rising": return "You're keeping more of each draft: \(o)% then, \(n)% lately."
        case "falling": return "You're changing more of each draft lately: \(o)% kept then, \(n)% now."
        default: return "About the same from week to week."
        }
    }
}

/// One quality dimension checked against real outcomes: where it is now,
/// the bounded next step, and which outcome drove it.
struct CalibrationDimension: Codable, Equatable {
    let `default`: Double?
    let suggested: Double?
    let nudgePct: Int?
    let reading: String?
    // Absent on an older server.
    var current: Double? = nil
    var stepPct: Int? = nil
    var explanation: String? = nil

    enum CodingKeys: String, CodingKey {
        case `default`, suggested, reading, current, explanation
        case nudgePct = "nudge_pct"
        case stepPct = "step_pct"
    }
}

/// How the edit predictor would have done on this restaurant's own past
/// drafts, each held out in turn — or why it cannot predict yet.
struct EditPredictionSummary: Codable, Equatable {
    struct Backtest: Codable, Equatable {
        let weeks: Int?
        let flagged: Int?
        let hits: Int?
        let hitRate: Double?
        let recall: Double?
        let baseRate: Double?
        enum CodingKeys: String, CodingKey {
            case weeks, flagged, hits, recall
            case hitRate = "hit_rate"
            case baseRate = "base_rate"
        }
    }
    let ready: Bool?
    let weeks: Int?
    let reason: String?
    let backtest: Backtest?

    /// "On your last 6 drafts, 71% of the rows it flagged were rows you
    /// changed — against 18% of all rows."
    var line: String? {
        guard ready == true else { return reason }
        guard let b = backtest, let hit = b.hitRate, let base = b.baseRate, (b.flagged ?? 0) > 0 else {
            return "Ready — it flags rows from your own edit history."
        }
        return "On your last \(b.weeks ?? weeks ?? 0) drafts, \(Int((hit * 100).rounded()))% of the rows it flagged were rows "
            + "you changed — against \(Int((base * 100).rounded()))% of all rows."
    }
}

/// Who is next for a weekend off, a close and a holiday, per role — the
/// multi-week rotation the draft is written and scored against.
struct RotationPlan: Codable, Equatable {
    let weeks: Int?
    let lines: [String]?
}

/// A sales-per-labor-hour target for one daypart across the week.
struct SplhTarget: Codable, Equatable {
    let target: Double?
    let history: Double?
    let source: String?
}

/// The sales-per-labor-hour objective the draft aims for.
struct SplhObjective: Codable, Equatable {
    let available: Bool?
    let reason: String?
    let targets: [String: SplhTarget]?
    let basis: String?
}

/// One borrowed starting figure: people in a role on a weekday's daypart.
struct BorrowedSlot: Codable, Identifiable, Equatable {
    let day: String
    let daypart: String
    let role: String
    let people: Int
    var id: String { "\(day)|\(daypart)|\(role)" }
}

/// A starting headcount borrowed from similar restaurants for a restaurant
/// with no history of its own — or why there is none.
struct StartingPoints: Codable, Equatable {
    let available: Bool?
    let ownHistory: Bool?
    let reason: String?
    let note: String?
    let cohortLabel: String?
    let n: Int?
    let bySlot: [BorrowedSlot]?
    enum CodingKeys: String, CodingKey {
        case available, reason, note, n
        case ownHistory = "own_history"
        case cohortLabel = "cohort_label"
        case bySlot = "by_slot"
    }
}

/// Whether the quality weights track this restaurant's outcomes yet.
/// Suggestions only — never applied on their own.
struct WeightCalibration: Codable, Equatable {
    let ready: Bool?
    let reason: String?
    let dimensions: [String: CalibrationDimension]?
    let note: String?
}

/// The offer to auto-publish, when the drafts have been going out
/// untouched and the latest scored well.
struct AutoPublishOffer: Codable, Equatable {
    let eligible: Bool
    let reason: String?
    let score: Double?
    // What stops Cavnar watching a shift, so no week can count as clean yet
    // ("no manager is set to receive issue texts"). Present only then.
    var missing: [String]? = nil
}

/// `GET labor/intel` — the record behind the draft. Every figure here is
/// measured from published weeks; nothing is written by a model.
struct ScheduleIntel: Codable, Equatable {
    let ok: Bool
    let error: String?
    let outcomes: [String: [String: IntelOutcome]]?
    let ledger: [String: IntelLedgerEntry]?
    let behaviour: [String: IntelBehaviour]?
    let couldHold: [String: [String]]?
    let mentored: [String: LooseValue]?
    let suggestedPairs: [SuggestedPair]?
    let splh: [String: [String: IntelSplh]]?
    let revenue: IntelRevenue?
    let suppressedRecommendationKinds: [String]?
    // What the engine is learning. Absent on an older server.
    let draftAcceptance: DraftAcceptance?
    let weightCalibration: WeightCalibration?
    var autoPublishOffer: AutoPublishOffer?
    // Schedule learning. Absent on an older server.
    var editPrediction: EditPredictionSummary? = nil
    var rotation: RotationPlan? = nil
    var splhObjective: SplhObjective? = nil
    var startingPoints: StartingPoints? = nil
    /// K8 — how demand forecasts have held up here, shown beside the
    /// projected revenue. Absent on an older server.
    var demandAccuracy: DemandAccuracy? = nil

    enum CodingKeys: String, CodingKey {
        case ok, error, outcomes, ledger, behaviour, mentored, splh, revenue, rotation
        case demandAccuracy = "demand_accuracy"
        case editPrediction = "edit_prediction"
        case splhObjective = "splh_objective"
        case startingPoints = "starting_points"
        case draftAcceptance = "draft_acceptance"
        case weightCalibration = "weight_calibration"
        case autoPublishOffer = "auto_publish_offer"
        case couldHold = "could_hold"
        case suggestedPairs = "suggested_pairs"
        case suppressedRecommendationKinds = "suppressed_recommendation_kinds"
    }

    var isEmpty: Bool {
        (outcomes ?? [:]).isEmpty && (ledger ?? [:]).isEmpty && (behaviour ?? [:]).isEmpty
            && (couldHold ?? [:]).isEmpty && (suggestedPairs ?? []).isEmpty && (splh ?? [:]).isEmpty
            && draftAcceptance?.available != true && autoPublishOffer?.eligible != true
            && (rotation?.lines ?? []).isEmpty && startingPoints?.available != true
    }
}

/// Everything the generator reads that is not the shift history itself:
/// who is on the roster and how they may be used, the pairs to keep
/// together or apart, the compliance rules and role floors, dated demand
/// signals, and the shifts staff have asked to hand back.
///
/// Separate from LaborViewModel because that one is the generation loop
/// and its result; this is the set-up around it, and LaborView keeps one
/// of each outside its Overview/Analytics branch so section state
/// survives a tab switch (see LaborViewModel.scheduleResultExpanded).
@Observable
@MainActor
final class ScheduleSetupViewModel {
    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private typealias OKResponse = APIClient.OKResponse

    // Expand/collapse for each section, lifted here for the same reason
    // LaborViewModel holds its own — see its comment.
    var rosterExpanded = false
    var demandExpanded = false
    var requestsExpanded = false

    // MARK: Roster

    var roster: [RosterMember] = []
    var pairs: [StaffPair] = []
    var suggestedPairs: [SuggestedPair] = []
    // Suggestions the owner waved away this session.
    var ignoredSuggestions: Set<String> = []
    var choices: RosterChoices?
    var canEditRoster = true
    var isLoadingRoster = false
    var rosterError: String?
    // Whose settings are mid-flight, so only that person's controls dim.
    var savingFor: String?
    // A one-line failure for the sheet's toast; cleared when the next save
    // starts. Optimistic writes roll back on their own before setting it.
    var settingsToast: String?

    var activeRoster: [RosterMember] { roster.filter(\.isActive) }
    var activeNames: [String] { activeRoster.map(\.name) }
    var employmentTypes: [String] { choices?.employmentType ?? ["full", "part"] }
    var dayparts: [String] { choices?.daypart ?? ["any", "morning", "night", "off"] }
    var days: [String] { choices?.days ?? LaborDayOfWeek.allNames }
    var certificationChoices: [String] { choices?.certifications ?? ruleCertifications }
    private struct PairRecEvent: Encodable {
        let key: String
        let event: String
        let kind: String?
        let surface = "labor"
        let module = "schedule"
    }
    private struct PairRecResponse: Decodable { let ok: Bool }

    /// "Ignore" holds on every device: the server stops suggesting the pair.
    func ignoreSuggestion(_ pair: SuggestedPair) {
        ignoredSuggestions.insert(pair.id)
        guard let key = pair.recKey else { return }
        Task {
            let _: PairRecResponse? = try? await client.send(
                "/mobile/api/recs/event", method: .post,
                body: PairRecEvent(key: key, event: "dismissed", kind: "not_for_us"), hapticOnError: false)
        }
    }

    /// Logged as taken when the pair is added from a suggestion.
    func addSuggestedPair(_ pair: SuggestedPair) async {
        if let key = pair.recKey {
            let _: PairRecResponse? = try? await client.send(
                "/mobile/api/recs/event", method: .post,
                body: PairRecEvent(key: key, event: "accepted", kind: nil), hapticOnError: false)
        }
        _ = await addPair(a: pair.a, b: pair.b, kind: pair.kind ?? "prefer", note: pair.evidence)
    }

    /// Suggested pairs not yet added or ignored.
    var openSuggestions: [SuggestedPair] {
        suggestedPairs.filter { s in
            !ignoredSuggestions.contains(s.id)
                && !pairs.contains { ($0.a == s.a && $0.b == s.b) || ($0.a == s.b && $0.b == s.a) }
        }
    }

    private struct RosterResponse: Decodable {
        let ok: Bool
        let roster: [RosterMember]?
        let pairs: [StaffPair]?
        let suggestedPairs: [SuggestedPair]?
        let choices: RosterChoices?
        let canEdit: Bool?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, roster, pairs, choices, error
            case suggestedPairs = "suggested_pairs"
            case canEdit = "can_edit"
        }
    }

    func loadRoster() async {
        isLoadingRoster = roster.isEmpty
        defer { isLoadingRoster = false }
        do {
            let r: RosterResponse = try await client.send("/mobile/api/labor/roster", hapticOnError: false)
            guard r.ok else { rosterError = r.error; return }
            roster = r.roster ?? []
            pairs = r.pairs ?? []
            suggestedPairs = r.suggestedPairs ?? []
            choices = r.choices
            canEditRoster = r.canEdit ?? true
            rosterError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if roster.isEmpty { rosterError = error.message }
        } catch {
            if roster.isEmpty { rosterError = "Couldn't load the roster." }
        }
    }

    /// One field of one person's settings — only what changed goes over
    /// the wire. Encoded by hand so an unset field is absent, not null.
    struct StaffSettingsPatch: Encodable {
        let employeeName: String
        var active: Bool? = nil
        var employmentType: String? = nil
        var minHours: Double? = nil
        var maxHours: Double? = nil
        var daypartAvailability: [String: String]? = nil
        var isMinor: Bool? = nil
        var timeWindows: [String: TimeWindow]? = nil
        var certifications: [String]? = nil
        var experienced: Bool? = nil
        /// "14-15", "16-17", or "" to clear.
        var minorAgeBand: String? = nil

        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case active, certifications, experienced
            case minorAgeBand = "minor_age_band"
            case employmentType = "employment_type"
            case minHours = "min_hours"
            case maxHours = "max_hours"
            case daypartAvailability = "daypart_availability"
            case isMinor = "is_minor"
            case timeWindows = "time_windows"
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(employeeName, forKey: .employeeName)
            try c.encodeIfPresent(active, forKey: .active)
            try c.encodeIfPresent(employmentType, forKey: .employmentType)
            try c.encodeIfPresent(minHours, forKey: .minHours)
            try c.encodeIfPresent(maxHours, forKey: .maxHours)
            try c.encodeIfPresent(daypartAvailability, forKey: .daypartAvailability)
            try c.encodeIfPresent(isMinor, forKey: .isMinor)
            try c.encodeIfPresent(timeWindows, forKey: .timeWindows)
            try c.encodeIfPresent(certifications, forKey: .certifications)
            try c.encodeIfPresent(experienced, forKey: .experienced)
            try c.encodeIfPresent(minorAgeBand, forKey: .minorAgeBand)
        }
    }

    private struct SettingsResponse: Decodable {
        let ok: Bool
        let settings: RosterSettings?
        let error: String?
    }

    /// Writes the change into the local row first and rolls it back on a
    /// refusal — this sheet is a run of switches and pickers, and a spinner
    /// between each would make setting up a team feel like filing forms.
    @discardableResult
    func updateSettings(_ patch: StaffSettingsPatch) async -> Bool {
        guard let index = roster.firstIndex(where: { $0.name == patch.employeeName }) else { return false }
        let previous = roster[index]
        var next = previous
        var settings = previous.settings ?? RosterSettings()
        if let v = patch.active { settings.active = v; next.active = v }
        if let v = patch.employmentType { settings.employmentType = v }
        if let v = patch.minHours { settings.minHours = v }
        if let v = patch.maxHours { settings.maxHours = v }
        if let v = patch.daypartAvailability { settings.daypartAvailability = v }
        if let v = patch.isMinor { settings.isMinor = v }
        if let v = patch.timeWindows { settings.timeWindows = v }
        if let v = patch.certifications { settings.certifications = v }
        if let v = patch.experienced { settings.experienced = v }
        if let v = patch.minorAgeBand { settings.minorAgeBand = v.isEmpty ? nil : v; if !v.isEmpty { settings.isMinor = true } }
        next.settings = settings
        roster[index] = next
        savingFor = patch.employeeName
        settingsToast = nil
        defer { savingFor = nil }
        do {
            let r: SettingsResponse = try await client.send(
                "/mobile/api/labor/staff-settings", method: .post, body: patch, hapticOnError: false)
            if r.ok {
                if let saved = r.settings, let i = roster.firstIndex(where: { $0.name == patch.employeeName }) {
                    roster[i].settings = saved
                    if let active = saved.active { roster[i].active = active }
                }
                Haptic.light()
                return true
            }
            rollBack(to: previous)
            settingsToast = r.error ?? "Couldn't save that."
        } catch let error as APIClient.APIError {
            rollBack(to: previous)
            settingsToast = error.message
        } catch {
            rollBack(to: previous)
            settingsToast = "Couldn't save that."
        }
        Haptic.error()
        return false
    }

    /// Puts one person back as they were before a refused edit — found by
    /// name again, because the roster can be reloaded while the save is in
    /// flight. The index captured before the await then pointed at someone
    /// else, and the rollback overwrote a new hire with the old row
    /// (CLIENT-32).
    private func rollBack(to previous: RosterMember) {
        guard let i = roster.firstIndex(where: { $0.name == previous.name }) else { return }
        roster[i] = previous
    }

    private struct PairBody: Encodable {
        let a: String
        let b: String
        let kind: String
        let note: String?
    }

    private struct PairResponse: Decodable {
        let ok: Bool
        let pair: StaffPair?
        let pairs: [StaffPair]?
        let error: String?
    }

    var isSavingPair = false
    var pairError: String?

    @discardableResult
    func addPair(a: String, b: String, kind: String, note: String?) async -> Bool {
        isSavingPair = true
        pairError = nil
        defer { isSavingPair = false }
        do {
            let r: PairResponse = try await client.send(
                "/mobile/api/labor/staff-pairs", method: .post,
                body: PairBody(a: a, b: b, kind: kind, note: note?.isEmpty == true ? nil : note))
            guard r.ok else { pairError = r.error ?? "Couldn't save that pair."; return false }
            if let all = r.pairs { pairs = all } else if let one = r.pair { pairs.append(one) }
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            pairError = error.message
        } catch {
            pairError = "Couldn't save that pair."
        }
        return false
    }

    func deletePair(id: Int) async {
        let previous = pairs
        pairs.removeAll { $0.id == id }
        do {
            let r: PairResponse = try await client.send(
                "/mobile/api/labor/staff-pairs/\(id)", method: .delete, hapticOnError: false)
            if r.ok {
                if let all = r.pairs { pairs = all }
                Haptic.selection()
            } else {
                pairs = previous
                pairError = r.error ?? "Couldn't remove that pair."
            }
        } catch let error as APIClient.APIError {
            pairs = previous
            pairError = error.message
        } catch {
            pairs = previous
            pairError = "Couldn't remove that pair."
        }
    }

    // MARK: Rules

    var rules: [String: LooseValue] = [:]
    var ruleDefaults: [String: LooseValue] = [:]
    var roleFloors: [String: RoleFloor] = [:]
    var ruleRoles: [String] = []
    var isLoadingRules = false
    var isSavingRules = false
    var rulesError: String?
    // The second batch: where the restaurant is, what that pack set, the
    // per-role arrivals and certifications, which roles are front of
    // house or on the patio, the budget trim, the reservation feed.
    var jurisdiction: String?
    var pack: CompliancePack?
    var packs: [CodeLabel] = []
    var roleArrivals: [String: Int] = [:]
    var roleRequirements: [String: [String]] = [:]
    var fohRoles: [String] = []
    var patioRoles: [String] = []
    /// Cross-training target per role, whole percents; a role left out
    /// uses `crossTrainingDefaults[role]` (else `crossTrainingDefault`).
    var roleCrossTraining: [String: Int] = [:]
    var crossTrainingDefaults: [String: Int] = [:]
    var crossTrainingDefault = 34
    var trimToBudget = true
    /// "Never cut a role below N people": the fewest people a send-home
    /// suggestion may leave in a role with no floor of its own
    /// (schedule_rules.cut_floor on the server). Cuts only, 1...cutFloorMax.
    var cutFloorDefault = 2
    var cutFloorMax = 10
    /// Whether this login may change the rules (the account owner).
    var canEditRules = true
    var ruleCertifications: [String] = []
    var reservationFeed: ReservationFeedStatus?
    var reservationProviders: [CodeLabel] = []
    var isSyncingReservations = false
    // The sync's own sentence — the honest 400 while nothing is live.
    var reservationSyncMessage: String?

    private struct RulesResponse: Decodable {
        let ok: Bool
        let rules: [String: LooseValue]?
        let defaults: [String: LooseValue]?
        let roleFloors: [String: RoleFloor]?
        let roles: [String]?
        let error: String?
        let jurisdiction: String?
        let pack: CompliancePack?
        let packs: [CodeLabel]?
        let roleArrivals: [String: Int]?
        let roleRequirements: [String: [String]]?
        let fohRoles: [String]?
        let patioRoles: [String]?
        let roleCrossTraining: [String: Int]?
        let crossTrainingDefaults: [String: Int]?
        let crossTrainingDefault: Int?
        let trimToBudget: Bool?
        let cutFloorDefault: Int?
        let cutFloorMax: Int?
        let canEdit: Bool?
        let certifications: [String]?
        let reservationFeed: ReservationFeedStatus?
        let reservationProviders: [CodeLabel]?
        enum CodingKeys: String, CodingKey {
            case ok, rules, defaults, roles, error, jurisdiction, pack, packs, certifications
            case roleFloors = "role_floors"
            case roleArrivals = "role_arrivals"
            case roleRequirements = "role_requirements"
            case fohRoles = "foh_roles"
            case patioRoles = "patio_roles"
            case roleCrossTraining = "role_cross_training"
            case crossTrainingDefaults = "cross_training_defaults"
            case crossTrainingDefault = "cross_training_default"
            case trimToBudget = "trim_to_budget"
            case cutFloorDefault = "cut_floor_default"
            case cutFloorMax = "cut_floor_max"
            case canEdit = "can_edit"
            case reservationFeed = "reservation_feed"
            case reservationProviders = "reservation_providers"
        }
    }

    /// Everything the rules sheet can save. Encoded by hand so a field
    /// that was not touched is absent, and the server leaves it alone.
    struct RulesPatch: Encodable {
        var rules: [String: LooseValue]? = nil
        var roleFloors: [String: RoleFloor]? = nil
        var jurisdiction: String?? = nil
        var roleArrivals: [String: Int]? = nil
        var roleRequirements: [String: [String]]? = nil
        var fohRoles: [String]? = nil
        var patioRoles: [String]? = nil
        var roleCrossTraining: [String: Int]? = nil
        var trimToBudget: Bool? = nil
        var cutFloorDefault: Int? = nil
        var reservationProvider: String?? = nil
        var reservationApiKey: String?? = nil

        enum CodingKeys: String, CodingKey {
            case rules, jurisdiction
            case roleFloors = "role_floors"
            case roleArrivals = "role_arrivals"
            case roleRequirements = "role_requirements"
            case fohRoles = "foh_roles"
            case patioRoles = "patio_roles"
            case roleCrossTraining = "role_cross_training"
            case trimToBudget = "trim_to_budget"
            case cutFloorDefault = "cut_floor_default"
            case reservationProvider = "reservation_provider"
            case reservationApiKey = "reservation_api_key"
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encodeIfPresent(rules, forKey: .rules)
            try c.encodeIfPresent(roleFloors, forKey: .roleFloors)
            // A double optional: `.some(nil)` means "clear it" and goes
            // over the wire as null; `nil` means untouched and is absent.
            if let j = jurisdiction { try c.encode(j, forKey: .jurisdiction) }
            try c.encodeIfPresent(roleArrivals, forKey: .roleArrivals)
            try c.encodeIfPresent(roleRequirements, forKey: .roleRequirements)
            try c.encodeIfPresent(fohRoles, forKey: .fohRoles)
            try c.encodeIfPresent(patioRoles, forKey: .patioRoles)
            try c.encodeIfPresent(roleCrossTraining, forKey: .roleCrossTraining)
            try c.encodeIfPresent(trimToBudget, forKey: .trimToBudget)
            try c.encodeIfPresent(cutFloorDefault, forKey: .cutFloorDefault)
            if let p = reservationProvider { try c.encode(p, forKey: .reservationProvider) }
            if let k = reservationApiKey { try c.encode(k, forKey: .reservationApiKey) }
        }
    }

    func loadRules() async {
        isLoadingRules = true
        defer { isLoadingRules = false }
        do {
            let r: RulesResponse = try await client.send("/mobile/api/labor/rules", hapticOnError: false)
            guard r.ok else { rulesError = r.error; return }
            rules = r.rules ?? [:]
            ruleDefaults = r.defaults ?? [:]
            roleFloors = r.roleFloors ?? [:]
            ruleRoles = r.roles ?? []
            jurisdiction = r.jurisdiction
            pack = r.pack
            packs = r.packs ?? []
            roleArrivals = r.roleArrivals ?? [:]
            roleRequirements = r.roleRequirements ?? [:]
            fohRoles = r.fohRoles ?? []
            patioRoles = r.patioRoles ?? []
            roleCrossTraining = r.roleCrossTraining ?? [:]
            crossTrainingDefaults = r.crossTrainingDefaults ?? [:]
            crossTrainingDefault = r.crossTrainingDefault ?? 34
            trimToBudget = r.trimToBudget ?? true
            cutFloorMax = max(1, r.cutFloorMax ?? 10)
            cutFloorDefault = min(max(r.cutFloorDefault ?? 2, 1), cutFloorMax)
            canEditRules = r.canEdit ?? true
            ruleCertifications = r.certifications ?? []
            reservationFeed = r.reservationFeed
            reservationProviders = r.reservationProviders ?? []
            rulesError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            rulesError = error.message
        } catch {
            rulesError = "Couldn't load the rules."
        }
    }

    @discardableResult
    func saveRules(_ newRules: [String: LooseValue], roleFloors newFloors: [String: RoleFloor]) async -> Bool {
        await saveRules(RulesPatch(rules: newRules, roleFloors: newFloors))
    }

    /// Save whatever the patch names, then read the rules back so the
    /// pack's applied values and the feed status are the server's own.
    @discardableResult
    func saveRules(_ patch: RulesPatch) async -> Bool {
        isSavingRules = true
        rulesError = nil
        defer { isSavingRules = false }
        do {
            let r: RulesResponse = try await client.send(
                "/mobile/api/labor/rules", method: .post, body: patch)
            guard r.ok else { rulesError = r.error ?? "Couldn't save the rules."; return false }
            if let v = r.rules { rules = v }
            if let v = r.roleFloors { roleFloors = v }
            if let v = r.roleArrivals { roleArrivals = v }
            if let v = r.roleRequirements { roleRequirements = v }
            if let v = r.fohRoles { fohRoles = v }
            if let v = r.patioRoles { patioRoles = v }
            if let v = r.roleCrossTraining { roleCrossTraining = v }
            if let v = r.trimToBudget { trimToBudget = v }
            if let v = r.cutFloorDefault { cutFloorDefault = v }
            if let v = r.reservationFeed { reservationFeed = v }
            // The pack and its applied values only come from a GET.
            await loadRules()
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            rulesError = error.message
        } catch {
            rulesError = "Couldn't save the rules."
        }
        return false
    }

    private struct SyncResponse: Decodable {
        let ok: Bool
        let error: String?
        let written: Int?
        let message: String?
    }

    /// Pull covers from the reservation system. Expect the server's own
    /// sentence back while no provider is live — that is the honest state.
    func syncReservations() async {
        isSyncingReservations = true
        reservationSyncMessage = nil
        defer { isSyncingReservations = false }
        do {
            let r: SyncResponse = try await client.send(
                "/mobile/api/labor/reservations/sync", method: .post, hapticOnError: false)
            if r.ok {
                reservationSyncMessage = r.message ?? "\(r.written ?? 0) dates written."
                Haptic.success()
                await loadSignals()
            } else {
                reservationSyncMessage = r.error ?? "The feed couldn't be read."
            }
        } catch let error as APIClient.APIError {
            reservationSyncMessage = error.message
        } catch {
            reservationSyncMessage = "The feed couldn't be read."
        }
    }

    // MARK: Learned patterns

    var learnedPatterns: [LearnedPattern] = []
    var canEditPatterns = true
    var patternBusyKey: String?
    var patternError: String?

    private struct PatternsResponse: Decodable {
        let ok: Bool
        let patterns: [LearnedPattern]?
        let canEdit: Bool?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, patterns, error
            case canEdit = "can_edit"
        }
    }

    private struct PatternBody: Encodable {
        let key: String
        let dismissed: Bool
    }

    func loadLearnedPatterns() async {
        do {
            let r: PatternsResponse = try await client.send("/mobile/api/labor/learned-patterns", hapticOnError: false)
            guard r.ok else { return }
            learnedPatterns = r.patterns ?? []
            canEditPatterns = r.canEdit ?? true
        } catch {
            // A secondary list; the roster stands without it.
        }
    }

    /// "Stop using this" / "Use again" — optimistic, rolled back on refusal.
    func setPattern(_ key: String, dismissed: Bool) async {
        guard let i = learnedPatterns.firstIndex(where: { $0.key == key }) else { return }
        let previous = learnedPatterns[i]
        learnedPatterns[i].dismissed = dismissed
        learnedPatterns[i].active = !dismissed && (previous.times ?? 0) >= 2
        patternBusyKey = key
        patternError = nil
        defer { patternBusyKey = nil }
        do {
            let r: OKResponse = try await client.send(
                "/mobile/api/labor/learned-patterns", method: .post,
                body: PatternBody(key: key, dismissed: dismissed), hapticOnError: false)
            if r.ok { Haptic.light() } else {
                learnedPatterns[i] = previous
                patternError = r.error ?? "Couldn't change that."
            }
        } catch let error as APIClient.APIError {
            learnedPatterns[i] = previous
            patternError = error.message
        } catch {
            learnedPatterns[i] = previous
            patternError = "Couldn't change that."
        }
    }

    // MARK: Intel — what the record says

    var intel: ScheduleIntel?
    var isLoadingIntel = false
    var intelError: String?
    var intelExpanded = false

    func loadIntel() async {
        isLoadingIntel = intel == nil
        defer { isLoadingIntel = false }
        do {
            let r: ScheduleIntel = try await client.send("/mobile/api/labor/intel", hapticOnError: false)
            guard r.ok else { intelError = r.error; return }
            intel = r
            intelError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if intel == nil { intelError = error.message }
        } catch {
            if intel == nil { intelError = "Couldn't read the record." }
        }
    }

    var isAcceptingAutoPublish = false
    var autoPublishError: String?

    private struct EnabledBody: Encodable { let enabled: Bool }

    /// Turn on auto-publish from the intel card's offer — the same call
    /// Account → Automation makes. The undo window still applies.
    func acceptAutoPublishOffer() async {
        isAcceptingAutoPublish = true
        autoPublishError = nil
        defer { isAcceptingAutoPublish = false }
        do {
            let r: OKResponse = try await client.send(
                "/mobile/api/labor/auto-publish", method: .post, body: EnabledBody(enabled: true), hapticOnError: false)
            guard r.ok else { autoPublishError = r.error ?? "Couldn't turn on auto-publish."; return }
            intel?.autoPublishOffer = AutoPublishOffer(eligible: false, reason: "Auto-publish is on.", score: nil)
            Haptic.success()
        } catch let error as APIClient.APIError {
            autoPublishError = error.message
        } catch {
            autoPublishError = "Couldn't turn on auto-publish."
        }
    }

    /// Apply the suggested quality weights — the owner's decision; the
    /// engine never changes them on its own.
    var isApplyingWeights = false
    var calibrationNotice: String?

    private struct EmptyBody: Encodable {}

    func applySuggestedWeights() async {
        isApplyingWeights = true
        calibrationNotice = nil
        defer { isApplyingWeights = false }
        do {
            let r: OKResponse = try await client.send(
                "/mobile/api/labor/quality/calibration/apply", method: .post, body: EmptyBody(), hapticOnError: false)
            calibrationNotice = r.ok ? "Applied — the next score uses these weights." : (r.error ?? "Couldn't apply them.")
            if r.ok { Haptic.success() }
        } catch let error as APIClient.APIError {
            calibrationNotice = error.message
        } catch {
            calibrationNotice = "Couldn't apply them."
        }
    }

    // MARK: Demand signals

    var signals: [DemandSignal] = []
    var isLoadingSignals = false
    var isSavingSignal = false
    var signalError: String?
    // What the last paste or add did, in the server's own count.
    var signalOutcome: String?

    private struct SignalsResponse: Decodable {
        let ok: Bool
        let signals: [DemandSignal]?
        let error: String?
    }

    private struct SignalRow: Encodable {
        let date: String
        let kind: String
        let label: String?
        let covers: Int?
        let liftPct: Double?
        enum CodingKeys: String, CodingKey {
            case date, kind, label, covers
            case liftPct = "lift_pct"
        }
    }

    private struct SignalRowsBody: Encodable { let rows: [SignalRow] }
    private struct SignalCSVBody: Encodable { let csv: String }

    private struct SignalWriteResponse: Decodable {
        let ok: Bool
        let written: Int?
        let skipped: Int?
        let errors: [String]?
        let error: String?
    }

    private static let isoDay: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = Calendar.current.timeZone
        return f
    }()

    /// The next 60 days, which is as far ahead as anybody schedules.
    func loadSignals() async {
        isLoadingSignals = signals.isEmpty
        defer { isLoadingSignals = false }
        let today = Calendar.current.startOfDay(for: Date())
        let end = Calendar.current.date(byAdding: .day, value: 60, to: today) ?? today
        do {
            let r: SignalsResponse = try await client.send(
                "/mobile/api/labor/demand-signals",
                query: ["start": Self.isoDay.string(from: today), "end": Self.isoDay.string(from: end)],
                hapticOnError: false)
            guard r.ok else { signalError = r.error; return }
            signals = (r.signals ?? []).sorted { $0.date < $1.date }
            signalError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if signals.isEmpty { signalError = error.message }
        } catch {
            if signals.isEmpty { signalError = "Couldn't load events and reservations." }
        }
    }

    @discardableResult
    func addSignal(date: Date, kind: String, label: String?, covers: Int?, liftPct: Double?) async -> Bool {
        let row = SignalRow(date: Self.isoDay.string(from: date), kind: kind,
                            label: label?.isEmpty == true ? nil : label, covers: covers, liftPct: liftPct)
        return await writeSignals(SignalRowsBody(rows: [row]))
    }

    @discardableResult
    func pasteSignals(csv: String) async -> Bool {
        await writeSignals(SignalCSVBody(csv: csv))
    }

    private func writeSignals(_ body: any Encodable) async -> Bool {
        isSavingSignal = true
        signalError = nil
        signalOutcome = nil
        defer { isSavingSignal = false }
        do {
            let r: SignalWriteResponse = try await client.send(
                "/mobile/api/labor/demand-signals", method: .post, body: body)
            guard r.ok else { signalError = r.error ?? "Couldn't save that."; return false }
            var parts: [String] = []
            parts.append("\(r.written ?? 0) written")
            if let s = r.skipped, s > 0 { parts.append("\(s) skipped") }
            if let e = r.errors, !e.isEmpty { parts.append("\(e.count) with errors") }
            signalOutcome = parts.joined(separator: " · ")
            if let e = r.errors, !e.isEmpty { signalError = e.prefix(3).joined(separator: "\n") }
            Haptic.success()
            await loadSignals()
            return true
        } catch let error as APIClient.APIError {
            signalError = error.message
        } catch {
            signalError = "Couldn't save that."
        }
        return false
    }

    func deleteSignal(id: Int) async {
        let previous = signals
        signals.removeAll { $0.id == id }
        do {
            let _: OKResponse = try await client.send(
                "/mobile/api/labor/demand-signals/\(id)", method: .delete, hapticOnError: false)
            Haptic.selection()
        } catch let error as APIClient.APIError {
            signals = previous
            signalError = error.message
        } catch {
            signals = previous
            signalError = "Couldn't remove that."
        }
    }

    // MARK: Shift requests

    var shiftRequests: [ShiftRequest] = []
    var openShifts: [ShiftRequest] = []
    var isLoadingRequests = false
    var requestBusyId: Int?
    var requestError: String?
    var pendingRequests: [ShiftRequest] { shiftRequests.filter { $0.status == "pending" } }

    private struct RequestsResponse: Decodable {
        let ok: Bool
        let requests: [ShiftRequest]?
        let open: [ShiftRequest]?
        let error: String?
    }

    private struct DecideBody: Encodable {
        let decision: String
        let replacement: String?
    }

    private struct DecideResponse: Decodable {
        let ok: Bool
        let request: ShiftRequest?
        let error: String?
    }

    func loadShiftRequests() async {
        isLoadingRequests = shiftRequests.isEmpty && openShifts.isEmpty
        defer { isLoadingRequests = false }
        do {
            let r: RequestsResponse = try await client.send("/mobile/api/labor/shift-requests", hapticOnError: false)
            guard r.ok else { requestError = r.error; return }
            shiftRequests = r.requests ?? []
            openShifts = r.open ?? []
            requestError = nil
            if !pendingRequests.isEmpty { requestsExpanded = true }
        } catch is CancellationError {
        } catch {
            // Silent, like time off: a secondary section.
        }
    }

    /// Approve or deny a hand-back. Naming a replacement on approve covers
    /// the shift outright; the server checks that person is legal for it
    /// and answers 400 with the reason when they are not.
    func decideShiftRequest(_ id: Int, approve: Bool, replacement: String? = nil) async {
        requestBusyId = id
        requestError = nil
        defer { requestBusyId = nil }
        do {
            let r: DecideResponse = try await client.send(
                "/mobile/api/labor/shift-requests/\(id)/decide", method: .post,
                body: DecideBody(decision: approve ? "approve" : "deny", replacement: replacement))
            if r.ok, let updated = r.request {
                if let i = shiftRequests.firstIndex(where: { $0.id == id }) { shiftRequests[i] = updated }
                Haptic.success()
                await loadShiftRequests()
            } else {
                requestError = r.error ?? "Couldn't decide that."
            }
        } catch let error as APIClient.APIError {
            requestError = error.message
        } catch {
            requestError = "Couldn't decide that."
        }
    }
}
