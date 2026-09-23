import Foundation
import Observation

/// Full weekday names as the backend's staff_availability rows store them
/// (Monday…Sunday) — shared by the availability save call (to compute the
/// "unavailable" complement of whichever days are checked) and by the
/// picker UI itself, so the two can never drift out of sync on spelling.
enum LaborDayOfWeek {
    static let allNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    static let shortLabels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
}

struct LaborOvertimeEntry: Codable, Identifiable {
    let employee: String?
    let hours: Double?
    let week: String?
    let status: String?
    // total_hours: the full actual-hours total across whatever shift data is
    // currently loaded (2 weeks for a typical CSV upload) — shown alongside
    // the single flagged week for the same "2-wk total" context the web
    // Labor tab's overtime alerts give.
    let totalHours: Double?
    // Whether this employee has a staff constraint note that explicitly
    // welcomes overtime (e.g. "happy to pick up extra hours") — computed
    // server-side via the same fuzzy name-matching the web dashboard uses,
    // so an "OT allowed" employee reads as a deliberate staffing choice
    // instead of a red flag.
    let otAllowed: Bool?
    var id: String { "\(employee ?? "")-\(week ?? "")" }

    enum CodingKeys: String, CodingKey {
        case employee, hours, week, status
        case totalHours = "total_hours"
        case otAllowed = "ot_allowed"
    }
}

struct LaborRoleSummary: Codable, Identifiable {
    let role: String
    let hours: Double
    let laborCost: Double
    let headcount: Int
    let laborPct: Double

    enum CodingKeys: String, CodingKey {
        case role, hours, headcount
        case laborCost = "labor_cost"
        case laborPct = "labor_pct"
    }

    var id: String { role }
}

struct LaborDateRange: Codable {
    let start: String?
    let end: String?
}

struct LaborOverstaffedDay: Codable, Identifiable {
    let date: String
    let day: String
    let laborPct: Double
    let laborCost: Double
    let sales: Double
    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, day, sales
        case laborPct = "labor_pct"
        case laborCost = "labor_cost"
    }
}

struct LaborUnderstaffedDay: Codable, Identifiable {
    let date: String
    let day: String
    let laborPct: Double
    let sales: Double
    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, day, sales
        case laborPct = "labor_pct"
    }
}

struct LaborSavingsBreakdown: Codable {
    let laborMonthly: Double
    let laborAnnual: Double
    let laborOvertime: Double
    let laborVsIndustryMonthly: Double
    let laborVsIndustryAnnual: Double

    enum CodingKeys: String, CodingKey {
        case laborMonthly = "labor_monthly"
        case laborAnnual = "labor_annual"
        case laborOvertime = "labor_overtime"
        case laborVsIndustryMonthly = "labor_vs_industry_monthly"
        case laborVsIndustryAnnual = "labor_vs_industry_annual"
    }
}

struct LaborUpcomingEvent: Codable, Identifiable {
    let name: String
    let dateStr: String
    let daysAway: Int
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case dateStr = "date_str"
        case daysAway = "days_away"
    }
}

struct StaffAvailabilityEntry: Codable, Identifiable, Equatable {
    let employeeName: String
    let availableDays: [String]
    let unavailableDays: [String]
    let notes: String?
    var id: String { employeeName }

    enum CodingKeys: String, CodingKey {
        case employeeName = "employee_name"
        case availableDays = "available_days"
        case unavailableDays = "unavailable_days"
        case notes
    }
}

/// One person on the roster, with whatever the owner has said about them.
///
/// The roster is derived from shift data rather than a staff table, because
/// that is the only place employees exist in this product — the same reason
/// availability, notes and contacts are all keyed by name.
struct RatedEmployee: Codable, Identifiable, Equatable {
    let name: String
    let role: String?
    let shifts: Int
    var score: Int?
    var scoreLabel: String?
    var notes: String?
    // Authorised to close. A fact about a person that owes nothing to their
    // rating, and the only way a leadership rule can be satisfied by
    // somebody the owner trusts to lock up but would not call a 5.
    var canClose: Bool?
    let updatedBy: String?
    let updatedAt: String?
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, role, shifts, score, notes
        case scoreLabel = "score_label"
        case canClose = "can_close"
        case updatedBy = "updated_by"
        case updatedAt = "updated_at"
    }
}

/// How much of the roster has been rated. `active` is false until somebody
/// is — the whole feature stays dormant until then, so a restaurant that
/// never touches it schedules exactly as it did before.
struct RatingCoverage: Codable, Equatable {
    let rated: Int
    let total: Int
    let unrated: [String]
    let active: Bool
    let pct: Int
}

/// A shift leader requirement: "Saturday dinner needs a bartender at 5."
struct ShiftLeaderRule: Codable, Identifiable, Equatable {
    var role: String
    var days: [String]?
    var daypart: String?
    var minScore: Double?
    var count: Int?

    var id: String {
        "\(role)|\((days ?? []).joined(separator: ","))|\(daypart ?? "")|\(minScore ?? 0)"
    }

    enum CodingKeys: String, CodingKey {
        case role, days, daypart, count
        case minScore = "min_score"
    }

    /// The rule as the owner would say it out loud.
    var sentence: String {
        let when = (days ?? []).isEmpty ? "Every" : (days ?? []).joined(separator: ", ")
        let part = (daypart ?? "").isEmpty ? "shift" : (daypart ?? "")
        let n = count ?? 1
        let who = n == 1 ? role.lowercased() : "\(n) \(role.lowercased())s"
        guard let min = minScore else { return "\(when) \(part): at least one \(who)." }
        let score = min == min.rounded() ? String(Int(min)) : String(format: "%.1f", min)
        return "\(when) \(part): at least \(n == 1 ? "one" : String(n)) \(who) scoring \(score) or above."
    }
}

/// One person inside a scheduled shift, as the strength check counted them.
struct StrengthMember: Codable, Equatable {
    let name: String
    let score: Double
}

/// A shift that came in under its combined-score target, with the reason.
struct StrengthShortfall: Codable, Identifiable, Equatable {
    let date: String
    let day: String?
    let daypart: String
    let role: String
    let strength: Double
    let target: Double
    let shortBy: Double?
    let reason: String
    let members: [StrengthMember]
    let unrated: [String]

    var id: String { "\(date)-\(daypart)-\(role)" }

    enum CodingKeys: String, CodingKey {
        case date, day, daypart, role, strength, target, reason, members, unrated
        case shortBy = "short_by"
    }
}

/// A shift leader requirement the finished schedule could not satisfy.
struct StrengthLeaderMiss: Codable, Identifiable, Equatable {
    let date: String
    let day: String?
    let daypart: String
    let role: String
    let rule: String
    let found: Int
    let reason: String

    var id: String { "\(date)-\(daypart)-\(role)-\(rule)" }
}

/// One dimension of Shift Quality — coverage, leadership, fatigue and the
/// rest. `facts` is deliberately not decoded: it carries the raw numbers
/// each dimension reached its verdict from, in a shape that differs per
/// dimension, and everything this UI shows is already in the sentences.
struct QualityDimension: Codable, Identifiable, Equatable {
    let key: String
    let label: String
    let score: Int
    let weight: Double?
    let strengths: [String]?
    let weaknesses: [String]?
    let customerFacing: Bool?
    // Present on the week-level roll-up rather than on a single shift.
    let shifts: Int?

    var id: String { key }
    var isCustomerFacing: Bool { customerFacing ?? true }

    enum CodingKeys: String, CodingKey {
        case key, label, score, weight, strengths, weaknesses, shifts
        case customerFacing = "customer_facing"
    }
}

/// What a shift was judged against. Monday lunch and Saturday dinner are
/// different jobs, and the profile is what says so.
struct QualityProfile: Codable, Equatable {
    let key: String
    let label: String
    let demand: String
    let minQuality: Int?
    let trainingAllowed: Bool?
    let source: String?

    enum CodingKeys: String, CodingKey {
        case key, label, demand, source
        case minQuality = "min_quality"
        case trainingAllowed = "training_allowed"
    }
}

/// A JSON value whose shape the client does not dictate — the `facts`
/// behind an assignment explanation differ per person (a score here, a
/// list of usual nights there). Decoded loosely and rendered as text.
enum LooseValue: Codable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case list([LooseValue])
    case object([String: LooseValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let l = try? c.decode([LooseValue].self) { self = .list(l) }
        else if let o = try? c.decode([String: LooseValue].self) { self = .object(o) }
        else { throw DecodingError.dataCorruptedError(in: c, debugDescription: "Unrecognised value") }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .list(let l): try c.encode(l)
        case .object(let o): try c.encode(o)
        case .null: try c.encodeNil()
        }
    }

    /// The value as a chip would print it. Nil for nothing worth a chip.
    var display: String? {
        switch self {
        case .string(let s): return s.isEmpty ? nil : s
        case .number(let n): return n == n.rounded() ? String(Int(n)) : String(format: "%.1f", n)
        case .bool(let b): return b ? "yes" : "no"
        case .list(let l):
            let parts = l.compactMap(\.display)
            return parts.isEmpty ? nil : parts.joined(separator: ", ")
        case .object, .null: return nil
        }
    }
}

/// Why one person landed on one shift — the engine's own sentence plus
/// the facts it built it from. Shown when a schedule row is tapped.
struct AssignmentExplanation: Codable, Identifiable, Equatable {
    let employee: String
    let role: String?
    let date: String?
    let day: String?
    let daypart: String?
    let why: String?
    let facts: [String: LooseValue]?

    var id: String { "\(date ?? "")-\(daypart ?? "")-\(employee)" }

    /// `facts` as "label · value" chips, in a stable order.
    var factChips: [String] {
        (facts ?? [:]).compactMap { key, value in
            guard let text = value.display else { return nil }
            let label = key.replacingOccurrences(of: "_", with: " ")
            return "\(label) · \(text)"
        }
        .sorted()
    }
}

/// One shift's verdict, with the reasons it reached it.
struct QualityShift: Codable, Identifiable, Equatable {
    let date: String
    let day: String
    let daypart: String
    let scored: Bool
    let score: Int?
    let band: String?
    let profile: QualityProfile
    let meetsProfile: Bool?
    let cappedBy: String?
    let headline: String?
    let people: [String]?
    let dimensions: [QualityDimension]?
    let strengths: [String]?
    let weaknesses: [String]?
    let blindSpots: [String]?
    // True when everything this shift had to say was also true of the rest
    // of the week, and so was hoisted into the week summary.
    let nothingSpecific: Bool?
    // Dimensions that raised rather than dimensions nobody configured. Kept
    // apart because only one of them is the owner's to act on.
    let failed: [QualityFailure]?
    // Who is on this shift and why each of them, from the engine. Absent
    // on a server that predates the explanation.
    let assignments: [AssignmentExplanation]?

    var id: String { "\(date)-\(daypart)" }

    /// "Saturday dinner" rather than a date — how a manager refers to it.
    var title: String {
        let part = ["morning": "lunch", "night": "dinner"][daypart] ?? daypart
        return day.isEmpty ? date : "\(day) \(part)"
    }

    enum CodingKeys: String, CodingKey {
        case date, day, daypart, scored, score, band, profile, headline, people
        case dimensions, strengths, weaknesses
        case meetsProfile = "meets_profile"
        case cappedBy = "capped_by"
        case blindSpots = "blind_spots"
        case nothingSpecific = "nothing_specific"
        case failed, assignments
    }
}

/// A dimension that threw while being computed.
struct QualityFailure: Codable, Identifiable, Equatable {
    let key: String
    let error: String?
    var id: String { key }
    var label: String {
        (key.replacingOccurrences(of: "_", with: " ")).prefix(1).uppercased()
        + key.replacingOccurrences(of: "_", with: " ").dropFirst()
    }
}

/// How much the engine actually knew when it scored the week. Deliberately
/// separate from the score itself: a 94 built on a fully rated roster is a
/// different claim from a 94 built on three ratings.
struct QualityConfidence: Codable, Equatable {
    let score: Int
    let level: String
    let reasons: [String]
    let summary: String

    var label: String { level.prefix(1).uppercased() + level.dropFirst() }
}

/// One alternative arrangement the engine tried, and what it bought.
struct WhatIfSwap: Codable, Identifiable, Equatable {
    struct Side: Codable, Equatable {
        let employee: String?
        let date: String?
        let day: String?
        let role: String?
    }
    let from: Side
    let to: Side
    let gain: Int
    let moved: [String]
    let reason: String

    var id: String { reason }
}

/// The what-if pass. Never a second generation — same headcount, same
/// hours, same roles, only who works which shift.
struct ScheduleWhatIf: Codable, Equatable {
    let ran: Bool
    let evaluated: Int?
    let baselineScore: Int?
    let bestScore: Int?
    let improvement: Int?
    let swaps: [WhatIfSwap]?
    let verdict: String?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case ran, evaluated, swaps, verdict, reason, improvement
        case baselineScore = "baseline_score"
        case bestScore = "best_score"
    }
}

/// A shift that came in under the bar its own profile sets.
struct QualityBelowProfile: Codable, Identifiable, Equatable {
    let date: String
    let day: String
    let daypart: String
    let score: Int
    let minQuality: Int
    let label: String

    var id: String { "\(date)-\(daypart)" }

    enum CodingKeys: String, CodingKey {
        case date, day, daypart, score, label
        case minQuality = "min_quality"
    }
}

/// Somebody the server says could legally take a shift: same role, free
/// that day, no staff constraint, no double booking, inside forty hours.
struct ScheduleReplacement: Codable, Identifiable, Equatable {
    let name: String
    let role: String?
    let score: Int?
    var id: String { name }
    var label: String { score.map { "\(name)  ·  \($0)" } ?? name }
}

/// The Shift Quality Engine's verdict on a whole week.
struct ScheduleQuality: Codable, Equatable {
    let checked: Bool
    let score: Int?
    let band: String?
    let shifts: [QualityShift]?
    let dimensions: [QualityDimension]?
    let belowProfile: [QualityBelowProfile]?
    let strengths: [String]?
    let weaknesses: [String]?
    let recommendations: [String]?
    let confidence: QualityConfidence?
    let best: String?
    let worst: String?
    let reason: String?
    // Recommendation kinds the engine left out because the owner never
    // acts on them. Absent on older payloads.
    let suppressedRecommendationKinds: [String]?
    // What the optimizer changed before the draft was shown, stored with
    // the week's quality. Absent on older payloads.
    let optimizer: ScheduleOptimizer?

    var scoredShifts: [QualityShift] { (shifts ?? []).filter { $0.scored } }
    var customerDimensions: [QualityDimension] {
        (dimensions ?? []).filter { $0.isCustomerFacing }
    }

    /// Low confidence makes the number provisional.
    var isProvisional: Bool { confidence?.level == "low" }

    /// True when the confidence reasons say most of the scheduled staff
    /// have no Operational Score — the prompt to rate them in place.
    var needsRatings: Bool {
        (confidence?.reasons ?? []).contains { $0.contains("have no Operational Score") }
    }

    enum CodingKeys: String, CodingKey {
        case checked, score, band, shifts, dimensions, strengths, weaknesses, optimizer
        case recommendations, confidence, best, worst, reason
        case belowProfile = "below_profile"
        case suppressedRecommendationKinds = "suppressed_recommendation_kinds"
    }

    /// The ledger's kind for a recommendation sentence, by its opening
    /// words — the same rule the server files them under.
    static func recommendationKind(_ text: String) -> String {
        let t = text.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("Fill the gap") { return "coverage" }
        if t.hasPrefix("Move somebody") { return "leadership" }
        if t.hasPrefix("Pair ") { return "strength" }
        if t.hasPrefix("Trim about") { return "hours" }
        if t.hasPrefix("Give ") { return "fatigue" }
        if t.hasPrefix("Rate the") { return "ratings" }
        return "other"
    }
}

/// One change the Shift Quality optimizer made to the draft, with why.
struct OptimizerChange: Codable, Identifiable, Equatable {
    let kind: String
    let reason: String
    let gain: Double?
    var id: String { "\(kind)-\(reason)" }
}

/// Something still wrong after the search, and why no legal change fixed
/// it — the owner's to decide.
struct OptimizerUnresolved: Codable, Identifiable, Equatable {
    let date: String?
    let day: String?
    let daypart: String?
    let dimension: String?
    let text: String
    var id: String { "\(date ?? "")-\(daypart ?? "")-\(dimension ?? "")-\(text)" }
}

/// The optimizer's summary: before and after, every change with why, and
/// what is left. From generation (`optimizer`), the stored quality
/// (`quality.optimizer`), or POST labor/schedule/optimize.
struct ScheduleOptimizer: Codable, Equatable {
    let ran: Bool
    let applied: Bool?
    let beforeScore: Int?
    let afterScore: Int?
    let improvement: Int?
    let changes: [OptimizerChange]?
    let unresolved: [OptimizerUnresolved]?
    let verdict: String?
    let stopped: String?

    enum CodingKeys: String, CodingKey {
        case ran, applied, improvement, changes, unresolved, verdict, stopped
        case beforeScore = "before_score"
        case afterScore = "after_score"
    }

    /// "Cavnar improved this draft from 71 to 84 — 5 changes", or nil when
    /// nothing changed.
    var headline: String? {
        let n = (changes ?? []).count
        guard n > 0, let before = beforeScore, let after = afterScore else { return nil }
        return "Cavnar improved this draft from \(before) to \(after) — \(n) \(n == 1 ? "change" : "changes")"
    }

    /// Worth a block at all: something changed, or something is left.
    var hasContent: Bool { ran && (!(changes ?? []).isEmpty || !(unresolved ?? []).isEmpty) }
}

/// The quality gate: when the draft was weak, the weakest days were
/// regenerated with what was wrong with them, and the better one kept.
struct ScheduleGate: Codable, Equatable {
    let ran: Bool
    let kept: String?
    let reason: String?
}

/// A rating stored under a name nobody on the roster has, with the
/// roster name it most likely means. GET labor/ratings/unmatched.
struct UnmatchedRating: Codable, Identifiable, Equatable {
    let rated: String
    let score: Double?
    let suggestion: String?
    let candidates: [String]?
    var id: String { rated }
}

/// A shift the generator removed to fit the budget, and why that one.
struct TrimmedShift: Codable, Identifiable, Equatable {
    let date: String?
    let day: String?
    let employee: String?
    let role: String?
    let shiftStart: String?
    let shiftEnd: String?
    let hours: Double?
    let reason: String?

    var id: String { "\(date ?? "")-\(employee ?? "")-\(shiftStart ?? "")" }

    enum CodingKeys: String, CodingKey {
        case date, day, employee, role, hours, reason
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
    }
}

/// A start moved later along the day's sales curve.
struct StaggeredStart: Codable, Identifiable, Equatable {
    let date: String?
    let employee: String?
    let role: String?
    let from: String?
    let to: String?
    let reason: String?

    var id: String { "\(date ?? "")-\(employee ?? "")-\(from ?? "")" }
}

/// What the week costs at the stated rates, overtime priced in.
struct ProjectedCost: Codable, Equatable {
    let straight: Double?
    let overtimePremium: Double?
    let overtimeHours: Double?
    let total: Double?
    let multiplier: Double?

    enum CodingKeys: String, CodingKey {
        case straight, total, multiplier
        case overtimePremium = "overtime_premium"
        case overtimeHours = "overtime_hours"
    }
}

/// How fresh the sales the forecast read were. `blind` when the newest
/// day is more than two weeks old — the draft is guessing from then on.
struct DemandDataThrough: Codable, Equatable {
    let date: String?
    let daysAgo: Int?
    let blind: Bool?

    enum CodingKeys: String, CodingKey {
        case date, blind
        case daysAgo = "days_ago"
    }
}

/// The reservation system's state, in the server's own sentence.
struct ReservationFeedStatus: Codable, Equatable {
    let provider: String?
    let label: String?
    let configured: Bool?
    let live: Bool?
    let message: String?
}

/// A holiday inside the week and the lift the record shows for it.
struct HolidayLift: Codable, Equatable {
    let name: String?
    let liftPct: Int?
    let basedOn: String?

    enum CodingKeys: String, CodingKey {
        case name
        case liftPct = "lift_pct"
        case basedOn = "based_on"
    }

    /// "Labor Day · +18%" or just the name when the lift is unknown.
    var label: String {
        guard let lift = liftPct else { return name ?? "Holiday" }
        return "\(name ?? "Holiday") · \(lift >= 0 ? "+" : "")\(lift)%"
    }
}

/// What an edit moves in hours and overtime-priced dollars, against the
/// rows the manager started from.
struct EditCostDelta: Codable, Equatable {
    let hoursBefore: Double?
    let hoursAfter: Double?
    let hoursDelta: Double?
    let dollarsBefore: Double?
    let dollarsAfter: Double?
    let dollarsDelta: Double?
    let overtimeHoursAfter: Double?

    enum CodingKeys: String, CodingKey {
        case hoursBefore = "hours_before"
        case hoursAfter = "hours_after"
        case hoursDelta = "hours_delta"
        case dollarsBefore = "dollars_before"
        case dollarsAfter = "dollars_after"
        case dollarsDelta = "dollars_delta"
        case overtimeHoursAfter = "overtime_hours_after"
    }

    /// "+6h · +$90 · 2h overtime". Nil when nothing moved.
    var summary: String? {
        let h = hoursDelta ?? 0
        let d = dollarsDelta ?? 0
        let ot = overtimeHoursAfter ?? 0
        guard h != 0 || d != 0 || ot > 0 else { return nil }
        var parts: [String] = []
        parts.append("\(h >= 0 ? "+" : "")\(h.commaFormatted)h")
        parts.append("\(d >= 0 ? "+" : "-")$\(abs(d).commaFormatted)")
        if ot > 0 { parts.append("\(ot.commaFormatted)h overtime") }
        return parts.joined(separator: " · ")
    }
}

/// The 409 a save answers with when somebody saved the week first.
struct SaveConflict: Decodable, Equatable {
    let conflict: Bool?
    let latestVersion: Int?
    let savedBy: String?
    let lines: [String]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case conflict, lines, error
        case latestVersion = "latest_version"
        case savedBy = "saved_by"
    }
}

/// The deterministic pass over the finished schedule. `checked` is false
/// when no targets and no leader rules are configured, which is the
/// default — nothing is claimed about a schedule nobody set targets for.
struct ScheduleStrength: Codable, Equatable {
    let checked: Bool
    let shortfalls: [StrengthShortfall]?
    let leaderMisses: [StrengthLeaderMiss]?

    var problems: Int { (shortfalls?.count ?? 0) + (leaderMisses?.count ?? 0) }
    var isClean: Bool { checked && problems == 0 }

    enum CodingKeys: String, CodingKey {
        case checked, shortfalls
        case leaderMisses = "leader_misses"
    }
}

struct LaborStats: Codable {
    let ok: Bool
    let isLive: Bool
    let overallLaborPct: Double
    let target: Double
    let onTrack: Bool
    let potentialSavings: Double
    let overtimeRisk: [LaborOvertimeEntry]
    let roleSummary: [LaborRoleSummary]
    let dateRange: LaborDateRange
    let overstaffedDays: [LaborOverstaffedDay]
    let understaffedDays: [LaborUnderstaffedDay]
    let dowSummary: [String: Double]
    let savingsBreakdown: LaborSavingsBreakdown
    let laborUpcoming: [LaborUpcomingEvent]
    // Everything the backend knows is incomplete about the upload behind
    // these numbers. labor.py has computed most of this since the
    // savings-formula work and no endpoint returned it, so a percentage
    // covering four of fourteen days looked exactly like one covering all
    // fourteen. Optional so an older server that omits them still decodes.
    let dataComplete: Bool?
    let analysisFailed: Bool?
    let hoursAreEstimated: Bool?
    let salesDataMissing: Bool?
    let daysMissingSales: [String]?
    let periodTooShortToProject: Bool?
    let periodDays: Int?
    let dataCaveat: String?

    /// One line naming what is incomplete, or nil when nothing is.
    var caveat: String? {
        guard let c = dataCaveat, !c.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return c
    }

    /// True only when these figures are a measured actual over a real period.
    var figuresAreTrustworthy: Bool { dataComplete ?? true }

    enum CodingKeys: String, CodingKey {
        case ok
        case isLive = "is_live"
        case overallLaborPct = "overall_labor_pct"
        case target
        case onTrack = "on_track"
        case potentialSavings = "potential_savings"
        case overtimeRisk = "overtime_risk"
        case roleSummary = "role_summary"
        case dateRange = "date_range"
        case overstaffedDays = "overstaffed_days"
        case understaffedDays = "understaffed_days"
        case dowSummary = "dow_summary"
        case savingsBreakdown = "savings_breakdown"
        case laborUpcoming = "labor_upcoming"
        case dataComplete = "data_complete"
        case analysisFailed = "analysis_failed"
        case hoursAreEstimated = "hours_are_estimated"
        case salesDataMissing = "sales_data_missing"
        case daysMissingSales = "days_missing_sales"
        case periodTooShortToProject = "period_too_short_to_project"
        case periodDays = "period_days"
        case dataCaveat = "data_caveat"
    }
}

struct ScheduleRow: Codable, Identifiable {
    let date: String?
    let day: String?
    var employee: String?
    let role: String?
    let shiftStart: String?
    let shiftEnd: String?
    let scheduledHours: String?
    let notes: String?
    // Set server-side only when a row's columns came back scrambled in a
    // way that couldn't be fully auto-repaired (day is always re-derived
    // from date server-side now, so this — not an unrecognized `day` value
    // — is the real signal that a row still needs a human look).
    var needsReview: Bool?
    // The rule the engine could not satisfy for this row, in words —
    // "approved time off", "under 10h rest after Monday close". Set with
    // `needsReview` by the compliance pass; a row flagged for scrambled
    // columns has no reason.
    var reviewReason: String?

    var id: String { "\(date ?? "")-\(employee ?? "")-\(shiftStart ?? "")" }

    enum CodingKeys: String, CodingKey {
        case date, day, employee, role, notes
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case scheduledHours = "scheduled_hours"
        case needsReview = "needs_review"
        case reviewReason = "review_reason"
    }
}

/// One swap the compliance pass made (or would make) to clear a violation.
struct ReviewFix: Codable, Identifiable, Equatable {
    let index: Int
    let from: String?
    let to: String?
    let kind: String?
    let reason: String?
    var id: String { "\(index)-\(from ?? "")-\(to ?? "")" }
}

/// A violation the pass could not clear by swapping anyone in.
struct ReviewUnfixed: Codable, Identifiable, Equatable {
    let index: Int
    let employee: String?
    let reason: String?
    var id: String { "\(index)-\(employee ?? "")" }
}

/// The deterministic compliance read over the finished week: how many
/// hard and soft rule breaks, the lines that say which, and what a fix
/// pass did or could not do about them.
struct ScheduleReview: Codable, Equatable {
    let hard: Int?
    let soft: Int?
    let byKind: [String: Int]?
    let lines: [String]?
    let hardRows: [Int]?
    let fixes: [ReviewFix]?
    let unfixed: [ReviewUnfixed]?
    // The budget trim and the staggered starts, repeated here so a
    // review loaded on its own still carries them.
    var trimmed: [TrimmedShift]? = nil
    var hoursTrimmed: Double? = nil
    var staggered: [StaggeredStart]? = nil

    var hardCount: Int { hard ?? 0 }
    var softCount: Int { soft ?? 0 }
    var isClean: Bool { hardCount == 0 && softCount == 0 }

    enum CodingKeys: String, CodingKey {
        case hard, soft, lines, fixes, unfixed, trimmed, staggered
        case byKind = "by_kind"
        case hardRows = "hard_rows"
        case hoursTrimmed = "hours_trimmed"
    }
}

/// One rule the schedule breaks, tied to the row it breaks it on.
struct RuleViolation: Codable, Identifiable, Equatable {
    let kind: String?
    let index: Int?
    let employee: String?
    let date: String?
    let day: String?
    let shiftStart: String?
    let role: String?
    let detail: String?
    let hard: Bool?
    let noShow: Bool?
    let label: String?

    var id: String { "\(kind ?? "")-\(index ?? -1)-\(employee ?? "")" }
    var isHard: Bool { hard ?? false }

    enum CodingKeys: String, CodingKey {
        case kind, index, employee, date, day, role, detail, hard, label
        case shiftStart = "shift_start"
        case noShow = "no_show"
    }
}

struct GeneratedSchedule: Codable {
    let ok: Bool
    let status: String?
    let summary: [String]?
    let weekDates: [String]?
    let weekDays: [String]?
    let hoursScheduled: Double?
    let laborTarget: Double?
    var previewRows: [ScheduleRow]?
    let scheduleCsv: String?
    let error: String?
    // PAR (per-average-round) hours budget — the AI's target hours/dollars
    // for the week vs. what actually got scheduled, same "PAR Hours Check"
    // banner the web schedule-preview panel shows. Already present in the
    // shared _run_schedule_job() result the web route has always returned;
    // just wasn't decoded on the iOS side until now.
    let hoursBudget: Double?
    let laborBudgetDollars: Double?
    let staffConstraints: [String: String]?
    // The Operational Score check the backend runs over the finished
    // schedule. Absent on a server that predates the feature, and
    // `checked: false` whenever no targets or leader rules are set.
    let strength: ScheduleStrength?
    // The Shift Quality Engine's verdict, and the alternatives it tried.
    var quality: ScheduleQuality?
    var whatIf: ScheduleWhatIf?
    // The schedule_history row this run was stored under — what an edit
    // saves against and what publishing sends. Absent on older payloads
    // still sitting in the on-device cache.
    let historyId: Int?
    // The compliance pass over the finished week and every rule it broke,
    // row by row. Both move after an edit or a fix pass.
    var review: ScheduleReview?
    var ruleViolations: [RuleViolation]?
    // Time off still waiting for an answer, by name — a warning, not a
    // block, because nobody has decided it yet.
    var pendingTimeOff: [String: [String]]?
    // The model's own short note. `summary` is the deterministic diff
    // against the last published week; this is the one paragraph it wrote.
    let narrative: String?
    let generationSeconds: Double?
    // True when the roster was too large for one pass and the week was
    // generated in date slices, then stitched.
    let chunked: Bool?
    // The names the engine actually scheduled from.
    let roster: [String]?
    // Set once the week has been sent to staff — history detail only.
    let publishedAt: String?
    let publishedBy: String?
    // History detail only: a save after sending re-emailed the people
    // whose shifts moved; a newer draft of the same week replaced this one.
    let republishedAt: String?
    let supersededBy: Int?
    // The stored what-if, as the column holds it (a JSON string).
    let whatIfJson: String?

    // MARK: Second batch — all optional so a cached payload still decodes.

    // Shifts removed to fit the budget, and how many hours they were.
    var trimmed: [TrimmedShift]?
    var hoursTrimmed: Double?
    // Starts moved later along the day's sales curve.
    var staggered: [StaggeredStart]?
    // The week priced at the stated rates; over-budget dollars when a
    // dollar budget exists, nil otherwise — never zero for "unknown".
    let projectedCost: ProjectedCost?
    let overBudgetDollars: Double?
    // Where the revenue the forecast scaled against came from.
    let projectedRevenueSource: String?
    // False when no intraday sales exist, so starts were not staggered.
    let hourlyProfileReady: Bool?
    let demandDataThrough: DemandDataThrough?
    let reservationFeed: ReservationFeedStatus?
    // Holidays inside the week, by ISO date.
    let holidayLift: [String: HolidayLift]?
    // Roles somebody has been trained up on, by name.
    let couldHold: [String: [String]]?
    // Non-empty when a very large roster was generated per department.
    let departments: [String]?
    // Set when only some days were regenerated; the rest were kept.
    let regeneratedDates: [String]?
    // What the optimizer changed before the draft was shown, and the
    // quality gate's verdict when it regenerated the weakest days. `var`
    // because Improve with Cavnar replaces the optimizer on screen.
    var optimizer: ScheduleOptimizer?
    let gate: ScheduleGate?

    enum CodingKeys: String, CodingKey {
        case ok, status, summary, error, strength, quality, review, narrative, chunked, roster
        case trimmed, staggered, departments, optimizer, gate
        case whatIf = "what_if"
        case weekDates = "week_dates"
        case weekDays = "week_days"
        case hoursScheduled = "hours_scheduled"
        case laborTarget = "labor_target"
        case previewRows = "preview_rows"
        case scheduleCsv = "schedule_csv"
        case hoursBudget = "hours_budget"
        case laborBudgetDollars = "labor_budget_dollars"
        case staffConstraints = "staff_constraints"
        case historyId = "history_id"
        case ruleViolations = "rule_violations"
        case pendingTimeOff = "pending_time_off"
        case generationSeconds = "generation_seconds"
        case publishedAt = "published_at"
        case publishedBy = "published_by"
        case republishedAt = "republished_at"
        case supersededBy = "superseded_by"
        case whatIfJson = "what_if_json"
        case hoursTrimmed = "hours_trimmed"
        case projectedCost = "projected_cost"
        case overBudgetDollars = "over_budget_dollars"
        case projectedRevenueSource = "projected_revenue_source"
        case hourlyProfileReady = "hourly_profile_ready"
        case demandDataThrough = "demand_data_through"
        case reservationFeed = "reservation_feed"
        case holidayLift = "holiday_lift"
        case couldHold = "could_hold"
        case regeneratedDates = "regenerated_dates"
    }

    /// The what-if the row stored, when the live key is absent — history
    /// detail hands the column back as a JSON string.
    var storedWhatIf: ScheduleWhatIf? {
        if let whatIf { return whatIf }
        guard let text = whatIfJson, !text.isEmpty, text != "null",
              let data = text.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(ScheduleWhatIf.self, from: data)
    }

    /// The optimizer summary to show: the live one, or the one stored
    /// with the week's quality.
    var optimizerSummary: ScheduleOptimizer? { optimizer ?? quality?.optimizer }

    /// The trim list from wherever it landed — the top level or the review.
    var trimmedShifts: [TrimmedShift] { trimmed ?? review?.trimmed ?? [] }
    var trimmedHours: Double { hoursTrimmed ?? review?.hoursTrimmed ?? 0 }
    var staggeredStarts: [StaggeredStart] { staggered ?? review?.staggered ?? [] }

    /// The holiday on a given ISO date, if the week has one there.
    func holiday(on date: String?) -> HolidayLift? {
        guard let date, let lifts = holidayLift else { return nil }
        return lifts[String(date.prefix(10))]
    }

    /// Every date in the week, in order, from the rows themselves.
    var rowDates: [String] {
        var seen: [String] = []
        for row in previewRows ?? [] {
            guard let d = row.date, !d.isEmpty, !seen.contains(d) else { continue }
            seen.append(d)
        }
        return seen.sorted()
    }

    /// The explanation for one row: the assignment on the same date whose
    /// employee matches, in the daypart the row's start time falls in
    /// (before 3pm is morning). Nil when the engine offered none.
    func explanation(for row: ScheduleRow) -> AssignmentExplanation? {
        guard let shifts = quality?.shifts, let date = row.date, let name = row.employee else { return nil }
        let daypart = Self.daypart(of: row.shiftStart)
        let sameDate = shifts.filter { $0.date == date }
        let ordered = sameDate.filter { $0.daypart == daypart } + sameDate.filter { $0.daypart != daypart }
        for shift in ordered {
            if let hit = (shift.assignments ?? []).first(where: {
                $0.employee.caseInsensitiveCompare(name) == .orderedSame
            }) { return hit }
        }
        return nil
    }

    /// "morning" for a start before 3pm, "night" otherwise — the same cut
    /// LaborView draws the day table with.
    static func daypart(of shiftStart: String?) -> String {
        guard let shiftStart else { return "night" }
        let f = DateFormatter()
        f.dateFormat = "h:mma"
        f.locale = Locale(identifier: "en_US_POSIX")
        guard let date = f.date(from: shiftStart.lowercased()) else { return "night" }
        let hour = Calendar.current.component(.hour, from: date)
        return hour < 15 ? "morning" : "night"
    }
}

@Observable
@MainActor
final class LaborViewModel {
    var stats: LaborStats?
    var isLoading = false
    var errorMessage: String?

    var isGeneratingSchedule = false
    var scheduleError: String?
    var scheduleResult: GeneratedSchedule?

    var availability: [StaffAvailabilityEntry] = []
    var isLoadingAvailability = false
    // Time-off requests from the staff portal (time_off.py), decided here.
    var timeOff: [TimeOffRequest] = []
    var timeOffExpanded = false
    var timeOffBusyId: Int?
    var timeOffError: String?
    var timeOffPending: Int { timeOff.filter { $0.status == "pending" }.count }
    var isSavingAvailability = false
    var availabilityError: String?

    // Expand/collapse state for the Overview tab's dropdown sections,
    // lifted up here (rather than left as each CavnarDropdown's own
    // internal @State) specifically because Overview/Analytics is an
    // if/else branch in LaborView — switching to Analytics and back tears
    // down and rebuilds the whole Overview branch, which would otherwise
    // reset every dropdown's local @State back to its startExpanded
    // default on every return trip. This view model instance sits outside
    // that branch and survives it, so the user's actual open/closed choice
    // sticks across tab switches instead of the schedule result silently
    // re-expanding (or an intentionally-opened section silently
    // re-collapsing) every time.
    var scheduleResultExpanded = true
    var overtimeExpanded = false
    var overstaffedExpanded = false
    var understaffedExpanded = false
    var availabilityExpanded = false
    var rolesExpanded = false
    var forecastExpanded = false
    var teamExpanded = false
    var targetsExpanded = false

    // MARK: Operational Score
    var team: [RatedEmployee] = []
    var teamCoverage: RatingCoverage?
    var teamThresholds: [String: Double] = [:]
    var leaderRules: [ShiftLeaderRule] = []
    var isLoadingTeam = false
    var teamError: String?
    // Set when the roster can't be built at all — no shift data uploaded
    // yet. A different state from an empty roster, and says what to do.
    var teamNote: String?
    // Whose rating is mid-flight. Scoped to one name rather than a global
    // flag so rating a second person doesn't grey out the first.
    var savingFor: String?
    var isSavingTargets = false
    // Targets an owner can set but the current team cannot reach. Saved
    // anyway — they may be describing the team they intend to have.
    var targetWarnings: [String] = []

    private let client: APIClient
    private var restaurantId: Int?

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// Loads whatever schedule/stats were last fetched and cached on this
    /// device for this restaurant, if any — called before the network
    /// load() so a relaunch (or a Face ID lock/unlock, which tears down and
    /// recreates this whole view model — see RootView's mainTabs comment)
    /// shows the last real result immediately instead of an empty Overview
    /// tab.
    ///
    /// Caching scheduleResult alone (the original fix here) turned out to
    /// be incomplete: LaborView only ever renders scheduleResultSection
    /// nested inside `if let stats = viewModel.stats`, and stats had no
    /// cache of its own — so a correctly-restored schedule still stayed
    /// invisible behind a fresh network fetch that had to complete (or
    /// fail visibly, kicking the whole Overview tab to an error/Retry
    /// state) before anything showed. That's what "the schedule keeps
    /// disappearing after I come back into the app" was actually reporting:
    /// scheduleResult was fine, stats just wasn't there yet to unlock it.
    /// Both need to survive together. No staleness check on stats, unlike
    /// the schedule below — it's a rolling snapshot with no "this was for a
    /// specific week" expiry, same as LaborAnalyticsViewModel's insight cache.
    func configureCaching(restaurantId: Int) {
        self.restaurantId = restaurantId
        if let data = SecureCache.read(key: Self.statsCacheKey(restaurantId)),
           let cached = try? Self.cacheDecoder.decode(LaborStats.self, from: data) {
            stats = cached
        }
        guard let data = SecureCache.read(key: Self.scheduleCacheKey(restaurantId)),
              let cached = try? Self.cacheDecoder.decode(GeneratedSchedule.self, from: data),
              !Self.isStale(cached) else { return }
        scheduleResult = cached
        if baselineRows == nil { baselineRows = cached.previewRows }
    }

    private static func scheduleCacheKey(_ restaurantId: Int) -> String { "labor.cachedSchedule.\(restaurantId)" }
    private static func statsCacheKey(_ restaurantId: Int) -> String { "labor.cachedStats.\(restaurantId)" }

    // Dedicated encoder/decoder for local caching (distinct from
    // JSONEncoder/Decoder.cavnar, which are for network payloads) —
    // tolerates NaN/Infinity in the budget/hours fields via
    // convertToString instead of the default .throw, which would
    // otherwise make encode() fail (silently, under the old `try?`) for
    // any restaurant configuration that produces a non-finite value
    // anywhere in the result, and the write would never happen at all.
    private static let cacheEncoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.nonConformingFloatEncodingStrategy = .convertToString(
            positiveInfinity: "inf", negativeInfinity: "-inf", nan: "nan"
        )
        return encoder
    }()

    private static let cacheDecoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.nonConformingFloatDecodingStrategy = .convertFromString(
            positiveInfinity: "inf", negativeInfinity: "-inf", nan: "nan"
        )
        return decoder
    }()

    /// A cached schedule for a week that's already ended isn't useful to
    /// resurrect — it'd read as "here's next week's plan" for a week that's
    /// now in the past.
    private static func isStale(_ schedule: GeneratedSchedule) -> Bool {
        guard let lastDateString = schedule.weekDates?.last else { return false }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        // Device-local calendar/timezone (not forced UTC) — this keeps the
        // parsed date in the same reference frame as Calendar.current
        // below. Parsing as UTC while comparing against device-local
        // "today" introduced a skew of several hours depending on the
        // device's own timezone, which the day-of-slack cutoff below
        // absorbs regardless.
        guard let lastDate = formatter.date(from: lastDateString) else { return false }
        guard let cutoff = Calendar.current.date(
            byAdding: .day, value: -1, to: Calendar.current.startOfDay(for: Date())
        ) else { return false }
        return lastDate < cutoff
    }

    // Not private — the round-trip test exercises this directly (not just
    // the raw Codable layer) to prove the actual method sequence the app
    // runs (configureCaching → generate → cacheSchedule → fresh instance →
    // configureCaching) works end to end, since a prior test that only
    // validated encode/decode in isolation didn't catch whatever's still
    // making the real generated schedule fail to survive a relaunch.
    func cacheSchedule(_ schedule: GeneratedSchedule) {
        guard let restaurantId, let data = try? Self.cacheEncoder.encode(schedule) else { return }
        SecureCache.write(data, key: Self.scheduleCacheKey(restaurantId))
    }

    // Not private, same reason as cacheSchedule above — exercised directly
    // by the round-trip test.
    func cacheStats(_ stats: LaborStats) {
        guard let restaurantId, let data = try? Self.cacheEncoder.encode(stats) else { return }
        SecureCache.write(data, key: Self.statsCacheKey(restaurantId))
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let fetched: LaborStats = try await client.send("/mobile/api/labor")
            stats = fetched
            cacheStats(fetched)
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
        } catch {
            errorMessage = "Couldn't load labor stats."
        }
        await reattachToRunningGeneration()
    }

    // MARK: - A generation that outlives the screen

    /// The job this restaurant's schedule generation is running under,
    /// kept outside the view model. LaborView builds a fresh view model per
    /// visit, and one that left mid-generation took the job id with it: the
    /// job finished server-side and the next visit showed a Generate button
    /// as if nothing had happened (CLIENT-27).
    private struct RunningGeneration: Codable {
        let jobId: String
        let startedAt: Date
        let dates: [String]
    }

    private static var runningGenerationKey: String { SessionScope.key("labor.runningGeneration") }
    /// Past the poll's own 15-minute budget, a remembered job is stale.
    private static let runningGenerationMaxAge: TimeInterval = 20 * 60

    private func rememberRunningGeneration(_ jobId: String, dates: [String]) {
        let entry = RunningGeneration(jobId: jobId, startedAt: Date(), dates: dates)
        if let data = try? JSONEncoder().encode(entry) {
            SecureCache.write(data, key: Self.runningGenerationKey)
        }
    }

    private func forgetRunningGeneration() {
        SecureCache.delete(key: Self.runningGenerationKey)
    }

    /// Picks a generation started on an earlier visit back up: one status
    /// check now (so a job that finished while the manager was away lands
    /// straight away), then the usual polling if it is still running.
    private func reattachToRunningGeneration() async {
        guard !isGeneratingSchedule,
              let data = SecureCache.read(key: Self.runningGenerationKey),
              let entry = try? JSONDecoder().decode(RunningGeneration.self, from: data) else { return }
        guard Date().timeIntervalSince(entry.startedAt) < Self.runningGenerationMaxAge else {
            forgetRunningGeneration()
            return
        }
        isGeneratingSchedule = true
        regeneratingDates = entry.dates
        await pollSchedule(jobId: entry.jobId, firstCheckWithoutWaiting: true)
    }

    private struct AvailabilityListResponse: Decodable {
        let ok: Bool
        let availability: [StaffAvailabilityEntry]
    }

    private struct AvailabilitySaveBody: Encodable {
        let employeeName: String
        let availableDays: [String]
        let unavailableDays: [String]
        let notes: String?

        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case availableDays = "available_days"
            case unavailableDays = "unavailable_days"
            case notes
        }
    }

    private struct EmployeeNameBody: Encodable {
        let employeeName: String
        enum CodingKeys: String, CodingKey { case employeeName = "employee_name" }
    }

    private typealias OkResponse = APIClient.OKResponse

    func loadAvailability() async {
        isLoadingAvailability = true
        defer { isLoadingAvailability = false }
        do {
            let response: AvailabilityListResponse = try await client.send("/mobile/api/labor/availability")
            availability = response.availability
        } catch {
            // Silent — the availability manager is a secondary section; a
            // failed fetch just leaves the list empty rather than blocking
            // the rest of the Overview tab with an error state.
        }
    }

    private struct TimeOffListResponse: Decodable { let ok: Bool; let requests: [TimeOffRequest] }
    private struct TimeOffDecideBody: Encodable { let decision: String }
    private struct TimeOffDecideResponse: Decodable { let ok: Bool; let request: TimeOffRequest?; let error: String? }

    func loadTimeOff() async {
        do {
            let r: TimeOffListResponse = try await client.send("/mobile/api/labor/time-off", hapticOnError: false)
            timeOff = r.requests
            if timeOffPending > 0 { timeOffExpanded = true }
        } catch {
            // Silent, like availability: a secondary section.
        }
    }

    func decideTimeOff(_ id: Int, approve: Bool) async {
        timeOffBusyId = id; timeOffError = nil
        defer { timeOffBusyId = nil }
        do {
            let r: TimeOffDecideResponse = try await client.send(
                "/mobile/api/labor/time-off/\(id)/decide", method: .post,
                body: TimeOffDecideBody(decision: approve ? "approve" : "deny"))
            if r.ok, let updated = r.request {
                if let i = timeOff.firstIndex(where: { $0.id == id }) { timeOff[i] = updated }
                await Haptic.success()
            } else {
                timeOffError = r.error ?? "Couldn't decide that."
            }
        } catch let error as APIClient.APIError {
            timeOffError = error.message
        } catch {
            timeOffError = "Couldn't decide that."
        }
    }

    /// Reuses the same day-name→short-label pairing the UI's picker uses,
    /// treats every unchecked day as explicitly unavailable — matches the
    /// web Availability Manager's own "checked = available, unchecked =
    /// unavailable" semantics (see saveAvailability() in dashboard.html).
    func saveAvailability(employeeName: String, availableDays: [String], notes: String?) async {
        let name = employeeName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        isSavingAvailability = true
        availabilityError = nil
        defer { isSavingAvailability = false }
        let unavailable = LaborDayOfWeek.allNames.filter { !availableDays.contains($0) }
        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/labor/availability", method: .post,
                body: AvailabilitySaveBody(
                    employeeName: name, availableDays: availableDays,
                    unavailableDays: unavailable, notes: notes?.trimmingCharacters(in: .whitespaces)
                )
            )
            if response.ok {
                Haptic.success()
                await loadAvailability()
            } else {
                availabilityError = response.error ?? "Couldn't save availability."
            }
        } catch let error as APIClient.APIError {
            availabilityError = error.message
        } catch {
            availabilityError = "Couldn't save availability."
        }
    }

    func deleteAvailability(employeeName: String) async {
        do {
            let _: OkResponse = try await client.send(
                "/mobile/api/labor/availability/delete", method: .post,
                body: EmployeeNameBody(employeeName: employeeName)
            )
            Haptic.selection()
            await loadAvailability()
        } catch {
            availabilityError = "Couldn't remove that entry."
        }
    }

    // MARK: - Manager overrides

    var isRescoringQuality = false
    // Who has been moved, so the UI can mark the rows a human changed and
    // the summary can say the score is no longer the generated one.
    var overriddenRows: Set<String> = []

    private struct ScoreBody: Encodable {
        let rows: [ScheduleRow]
        let dailyTargetHours: [String: Double]
        let save: Bool
        let historyId: Int?
        // The version the rows were loaded from. The server refuses with a
        // 409 when a newer one is on file, so two managers editing the
        // same week never silently overwrite each other.
        let version: Int?
        enum CodingKeys: String, CodingKey {
            case rows, save, version
            case dailyTargetHours = "daily_target_hours"
            case historyId = "history_id"
        }
    }

    private struct ScoreResponse: Decodable {
        let ok: Bool
        let quality: ScheduleQuality?
        let whatIf: ScheduleWhatIf?
        let saved: Bool?
        let error: String?
        // The compliance read over the rows as edited — every save answers
        // with the rules the new week breaks, so the panel never shows a
        // verdict for rows that are no longer on screen.
        let violations: [RuleViolation]?
        let review: ScheduleReview?
        let pendingTimeOff: [String: [String]]?
        // After a save of a PUBLISHED week: the people re-emailed because
        // their own shifts moved. Nil on a draft; empty when nobody's did.
        let changedSinceSent: [String]?
        enum CodingKeys: String, CodingKey {
            case ok, quality, error, saved, violations, review
            case whatIf = "what_if"
            case pendingTimeOff = "pending_time_off"
            case changedSinceSent = "changed_since_sent"
        }
    }

    private struct RowsBody: Encodable { let rows: [ScheduleRow] }

    // MARK: - Versions, conflicts and the change notice

    // The newest version on file for the week on screen — what a save
    // sends so the server can tell whether somebody else got there first.
    var latestVersion: Int?
    // Set from a 409: somebody saved after this draft was opened. The
    // sheet shows their lines and offers Reload; nothing is overwritten.
    var saveConflict: SaveConflict?
    // "Updated schedule sent to Ana, Bob" / "Saved; nobody's shifts
    // changed" — only after a save of a week staff already have.
    var saveNotice: String?
    var isReloadingAfterConflict = false

    private struct VersionsEnvelope: Decodable {
        let ok: Bool
        let versions: [ScheduleVersion]?
    }

    /// The latest version number for the week on screen. Silent on
    /// failure: a save then goes without one and the server skips the
    /// check, which is the pre-versions behaviour, not a new failure.
    func loadLatestVersion() async {
        guard let id = scheduleResult?.historyId else { latestVersion = nil; return }
        do {
            let r: VersionsEnvelope = try await client.send(
                "/mobile/api/labor/schedule-history/\(id)/versions", hapticOnError: false)
            latestVersion = (r.versions ?? []).map(\.version).max()
        } catch {
            // Keep whatever was known.
        }
    }

    /// Throw away the local edits and take the week as it is on file —
    /// the only way out of a conflict that never overwrites.
    func reloadAfterConflict() async {
        guard let id = scheduleResult?.historyId else { saveConflict = nil; return }
        isReloadingAfterConflict = true
        defer { isReloadingAfterConflict = false }
        do {
            let fresh: GeneratedSchedule = try await client.send(
                "/mobile/api/labor/schedule-history/\(id)", hapticOnError: false)
            scheduleResult = fresh
            baselineRows = fresh.previewRows
            editCost = nil
            overriddenRows = []
            hasUnsavedFixes = false
            scoreDelta = nil
            optimizerUnsaved = false
            overrideState = .idle
            saveConflict = nil
            cacheSchedule(fresh)
            await loadLatestVersion()
            Haptic.success()
        } catch let error as APIClient.APIError {
            overrideState = .failed(error.message)
        } catch {
            overrideState = .failed("Couldn't reload this week.")
        }
    }

    // MARK: - Cost of an edit

    // The rows the manager started from — set when a week lands or is
    // reloaded, never on save, so the readout stays "against the draft".
    var baselineRows: [ScheduleRow]?
    var editCost: EditCostDelta?

    private struct ViolationsBody: Encodable {
        let rows: [ScheduleRow]
        let baselineRows: [ScheduleRow]
        enum CodingKeys: String, CodingKey {
            case rows
            case baselineRows = "baseline_rows"
        }
    }

    private struct ViolationsResponse: Decodable {
        let ok: Bool
        let cost: EditCostDelta?
    }

    /// "+6h · +$90 · 2h overtime" for the rows on screen against the
    /// baseline. Read-only; nothing is stored. Silent on failure.
    func refreshEditCost() async {
        guard let rows = scheduleResult?.previewRows, let base = baselineRows, !rows.isEmpty else {
            editCost = nil
            return
        }
        do {
            let r: ViolationsResponse = try await client.send(
                "/mobile/api/labor/schedule/violations", method: .post,
                body: ViolationsBody(rows: rows, baselineRows: base), hapticOnError: false)
            if r.ok { editCost = r.cost }
        } catch {
            // The readout is a courtesy; the save path reports its own errors.
        }
    }

    // MARK: - Recommendations ledger

    // What the owner said about each recommendation this session, by its
    // text — "accepted" or "dismissed" — so the ✓ / ✕ stays put.
    var recommendationDecisions: [String: String] = [:]
    var suppressedRecommendationKinds: [String] = []

    private struct RecommendationBody: Encodable {
        let kind: String
        let key: String
        let action: String
    }

    private struct RecommendationResponse: Decodable {
        let ok: Bool
        let suppressedKinds: [String]?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, error
            case suppressedKinds = "suppressed_kinds"
        }
    }

    /// Record "did it" or "not for us" — the ledger that decides which
    /// kinds keep being shown.
    func recordRecommendation(_ text: String, accepted: Bool) async {
        let action = accepted ? "accepted" : "dismissed"
        let previous = recommendationDecisions[text]
        recommendationDecisions[text] = action
        do {
            let r: RecommendationResponse = try await client.send(
                "/mobile/api/labor/schedule/recommendation", method: .post,
                body: RecommendationBody(kind: ScheduleQuality.recommendationKind(text),
                                         key: String(text.prefix(200)), action: action),
                hapticOnError: false)
            if r.ok {
                suppressedRecommendationKinds = r.suppressedKinds ?? suppressedRecommendationKinds
                Haptic.light()
            } else {
                recommendationDecisions[text] = previous
            }
        } catch {
            recommendationDecisions[text] = previous
        }
    }

    // MARK: - Redo selected days

    // ISO dates ticked in the generated week for "Redo selected days".
    var selectedRedoDates: Set<String> = []

    func toggleRedoDate(_ date: String) {
        if selectedRedoDates.contains(date) { selectedRedoDates.remove(date) } else { selectedRedoDates.insert(date) }
        Haptic.selection()
    }

    /// Regenerate only the ticked days of the draft on screen; the rest
    /// are kept. Same job and polling as a full generation.
    func redoSelectedDays() async {
        guard let id = scheduleResult?.historyId, !selectedRedoDates.isEmpty else { return }
        let dates = selectedRedoDates.sorted()
        selectedRedoDates = []
        await generateSchedule(dates: dates, historyId: id)
    }

    private struct ApplyFixesResponse: Decodable {
        let ok: Bool
        let rows: [ScheduleRow]?
        let fixes: [ReviewFix]?
        let unfixed: [ReviewUnfixed]?
        let quality: ScheduleQuality?
        let review: ScheduleReview?
        let violations: [RuleViolation]?
        let error: String?
    }

    var isApplyingFixes = false
    var applyFixesError: String?
    // Rows the fix pass replaced but nobody has saved yet. The owner still
    // chooses — a fix is a proposal until Save sends it.
    var hasUnsavedFixes = false

    /// Ask the engine to clear what it can — every hard violation it can
    /// swap somebody legal into — and show the result. Nothing is stored
    /// until the owner saves: the rows on screen change, the score and the
    /// review re-render, and Save is what commits them.
    func applyFixes() async {
        guard var result = scheduleResult, let rows = result.previewRows, !rows.isEmpty else { return }
        isApplyingFixes = true
        applyFixesError = nil
        defer { isApplyingFixes = false }
        do {
            let response: ApplyFixesResponse = try await client.send(
                "/mobile/api/labor/schedule/apply-fixes", method: .post,
                body: RowsBody(rows: rows), hapticOnError: false, retryTransient: false)
            guard response.ok else {
                applyFixesError = response.error ?? "Couldn't apply those fixes."
                return
            }
            if let fixed = response.rows {
                // Mark the rows a fix moved so the table shows CHANGED on
                // exactly the people who are different now.
                for fix in response.fixes ?? [] where fixed.indices.contains(fix.index) {
                    overriddenRows.insert(fixed[fix.index].id)
                }
                result.previewRows = fixed
            }
            if let quality = response.quality {
                scoreDelta = Self.delta(from: result.quality?.score, to: quality.score)
                result.quality = quality
            }
            if let review = response.review {
                result.review = review
            } else if var review = result.review {
                review = ScheduleReview(hard: review.hard, soft: review.soft, byKind: review.byKind,
                                        lines: review.lines, hardRows: review.hardRows,
                                        fixes: response.fixes ?? review.fixes,
                                        unfixed: response.unfixed ?? review.unfixed)
                result.review = review
            }
            if let violations = response.violations { result.ruleViolations = violations }
            scheduleResult = result
            hasUnsavedFixes = !(response.fixes ?? []).isEmpty
            overrideState = .idle
            Haptic.success()
            await refreshEditCost()
        } catch let error as APIClient.APIError {
            applyFixesError = error.message
        } catch {
            applyFixesError = "Couldn't apply those fixes."
        }
    }

    // MARK: - Improve with Cavnar, the live score, what-if

    /// Points the score moved on the last edit, fix pass, improvement or
    /// re-score — "+3" / "−2" beside the number. Nil before any.
    var scoreDelta: Int?
    var isOptimizing = false
    var optimizeError: String?
    /// True while Cavnar's changes are on screen and not yet saved.
    var optimizerUnsaved = false

    nonisolated static func delta(from before: Int?, to after: Int?) -> Int? {
        guard let before, let after else { return nil }
        return after - before
    }

    private struct OptimizeBody: Encodable { let rows: [ScheduleRow] }

    private struct OptimizeResponse: Decodable {
        let ok: Bool
        let rows: [ScheduleRow]?
        let optimizer: ScheduleOptimizer?
        let quality: ScheduleQuality?
        let whatIf: ScheduleWhatIf?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, rows, optimizer, quality, error
            case whatIf = "what_if"
        }
    }

    /// POST labor/schedule/optimize: the Shift Quality repair loop over the
    /// week on screen. Nothing is stored — the improved rows are shown with
    /// every change and why, and Save (the same one Apply fixes uses) is
    /// what keeps them. Rows Cavnar touched carry a note starting "Cavnar:".
    func optimize() async {
        guard var result = scheduleResult, let rows = result.previewRows, !rows.isEmpty else { return }
        isOptimizing = true
        optimizeError = nil
        defer { isOptimizing = false }
        do {
            let response: OptimizeResponse = try await client.send(
                "/mobile/api/labor/schedule/optimize", method: .post,
                body: OptimizeBody(rows: rows), hapticOnError: false, timeout: 45, retryTransient: false)
            guard response.ok else {
                optimizeError = response.error ?? "Couldn't improve the draft."
                return
            }
            let changes = response.optimizer?.changes ?? []
            result.optimizer = response.optimizer
            if !changes.isEmpty, let improved = response.rows {
                for row in improved where (row.notes ?? "").hasPrefix("Cavnar:") { overriddenRows.insert(row.id) }
                result.previewRows = improved
                if let quality = response.quality {
                    scoreDelta = Self.delta(from: result.quality?.score, to: quality.score)
                    result.quality = quality
                }
                result.whatIf = response.whatIf ?? result.whatIf
                hasUnsavedFixes = true
                optimizerUnsaved = true
                overrideState = .idle
            }
            scheduleResult = result
            Haptic.success()
            if !changes.isEmpty { await refreshEditCost() }
        } catch let error as APIClient.APIError {
            optimizeError = error.message
        } catch {
            optimizeError = "Couldn't improve the draft."
        }
    }

    /// What scoring a set of rows says, without storing anything.
    struct LiveScore {
        let quality: ScheduleQuality
        let hardRules: Int
    }

    /// Score rows with save:false — the what-if and the re-score after a
    /// rating. The same endpoint Save uses; nothing is written.
    func liveScore(rows: [ScheduleRow]) async -> LiveScore? {
        do {
            let response: ScoreResponse = try await client.send(
                "/mobile/api/labor/schedule/score", method: .post,
                body: ScoreBody(rows: rows, dailyTargetHours: [:], save: false, historyId: nil, version: nil),
                hapticOnError: false, retryTransient: true)
            guard response.ok, let quality = response.quality else { return nil }
            return LiveScore(quality: quality, hardRules: response.review?.hardCount ?? 0)
        } catch {
            return nil
        }
    }

    /// Re-score the rows on screen without saving — after rating somebody
    /// from the quality panel, so the number moves with the ratings.
    func rescoreLive() async {
        guard var result = scheduleResult, let rows = result.previewRows, !rows.isEmpty else { return }
        isRescoringQuality = true
        defer { isRescoringQuality = false }
        guard let live = await liveScore(rows: rows) else {
            overrideState = .failed("Couldn't re-score the week.")
            return
        }
        scoreDelta = Self.delta(from: result.quality?.score, to: live.quality.score)
        result.quality = live.quality
        scheduleResult = result
    }

    /// The rows one shift is made of: same date, same daypart.
    func rows(for shift: QualityShift) -> [ScheduleRow] {
        (scheduleResult?.previewRows ?? []).filter {
            $0.date == shift.date && GeneratedSchedule.daypart(of: $0.shiftStart) == shift.daypart
        }
    }

    /// Everybody who could be tried on a shift: the roster the engine drew
    /// from and the rated team, minus whoever is already on it.
    func whatIfCandidates(for shift: QualityShift) -> [String] {
        let on = Set(rows(for: shift).compactMap { $0.employee?.lowercased() })
        var seen = Set<String>(), out: [String] = []
        for name in (scheduleResult?.roster ?? []) + team.map(\.name) {
            let n = name.trimmingCharacters(in: .whitespaces)
            guard !n.isEmpty, !on.contains(n.lowercased()), seen.insert(n.lowercased()).inserted else { continue }
            out.append(n)
        }
        return out.sorted()
    }

    /// The week's rows with `who` on this shift — in place of the row
    /// `replacing` when given, otherwise added alongside the first row.
    func whatIfRows(shift: QualityShift, who: String, replacing rowId: String?) -> [ScheduleRow]? {
        guard var rows = scheduleResult?.previewRows else { return nil }
        if let rowId, let i = rows.firstIndex(where: { $0.id == rowId }) {
            rows[i].employee = who
            return rows
        }
        guard let base = self.rows(for: shift).first else { return nil }
        let role = team.first { $0.name == who }?.role ?? base.role
        rows.append(ScheduleRow(date: base.date, day: base.day, employee: who, role: role,
                                shiftStart: base.shiftStart, shiftEnd: base.shiftEnd,
                                scheduledHours: base.scheduledHours, notes: nil))
        return rows
    }

    /// Put a what-if on the week: the rows replace the draft and are saved
    /// exactly like any other override.
    func applyWhatIf(_ rows: [ScheduleRow], who: String, date: String) async {
        guard var result = scheduleResult else { return }
        result.previewRows = rows
        scheduleResult = result
        for row in rows where row.employee == who && row.date == date { overriddenRows.insert(row.id) }
        Haptic.light()
        await rescoreQuality()
        await refreshEditCost()
    }

    // MARK: - Rating in place

    /// Ratings given from the quality panel this session, so a row stays
    /// on screen with its answer rather than vanishing under the thumb.
    var ratedInPrompt: [String: Int] = [:]

    /// The people carrying the most hours this week who have no
    /// Operational Score — only when the confidence says most of the week
    /// is unrated. At most ten.
    struct UnratedPerson: Identifiable, Equatable {
        let name: String
        let hours: Double
        var id: String { name }
    }

    var unratedByHours: [UnratedPerson] {
        guard let q = scheduleResult?.quality, q.needsRatings, !team.isEmpty,
              let rows = scheduleResult?.previewRows else { return [] }
        let scored = Set(team.filter { $0.score != nil }.map { $0.name.lowercased() })
        var hours: [String: Double] = [:]
        var order: [String] = []
        for row in rows {
            let name = (row.employee ?? "").trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty else { continue }
            if hours[name] == nil { order.append(name) }
            hours[name, default: 0] += Double(row.scheduledHours ?? "") ?? 0
        }
        let people = order
            .filter { !scored.contains($0.lowercased()) || ratedInPrompt[$0] != nil }
            .map { UnratedPerson(name: $0, hours: hours[$0] ?? 0) }
            .sorted { $0.hours > $1.hours }
        return Array(people.prefix(10))
    }

    /// Rate somebody from the quality panel — the same endpoint the
    /// Operational Score list uses. Works for a name the team list does
    /// not carry yet.
    func rateFromPrompt(_ name: String, score: Int) async {
        let previous = ratedInPrompt[name]
        ratedInPrompt[name] = score
        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/labor/team/rating", method: .post,
                body: RatingBody(employeeName: name, score: score, notes: nil), hapticOnError: false)
            if response.ok {
                if let i = team.firstIndex(where: { $0.name == name }) {
                    team[i].score = score
                    team[i].scoreLabel = Self.scoreLabels[score]
                    recountCoverage()
                }
                Haptic.light()
            } else {
                ratedInPrompt[name] = previous
                teamError = response.error ?? "Couldn't save that rating."
            }
        } catch let error as APIClient.APIError {
            ratedInPrompt[name] = previous
            teamError = error.message
        } catch {
            ratedInPrompt[name] = previous
            teamError = "Couldn't save that rating."
        }
    }

    // MARK: - Ratings that match nobody

    var unmatchedRatings: [UnmatchedRating] = []
    var matchingRating: String?
    var matchError: String?

    private struct UnmatchedResponse: Decodable {
        let ok: Bool
        let unmatched: [UnmatchedRating]?
    }

    private struct MatchBody: Encodable {
        let rated: String
        let rosterName: String
        enum CodingKeys: String, CodingKey {
            case rated
            case rosterName = "roster_name"
        }
    }

    /// GET labor/ratings/unmatched. Silent on failure: the list is a
    /// courtesy on top of the ratings, never a blocker.
    func loadUnmatchedRatings() async {
        do {
            let r: UnmatchedResponse = try await client.send("/mobile/api/labor/ratings/unmatched", hapticOnError: false)
            unmatchedRatings = r.ok ? (r.unmatched ?? []) : []
        } catch {
            // Keep whatever was known.
        }
    }

    /// Move a rating onto a roster name. A 409 (the roster name already
    /// has one) comes back as the server's own sentence.
    func matchRating(_ rated: String, to rosterName: String) async {
        matchingRating = rated
        matchError = nil
        defer { matchingRating = nil }
        do {
            let r: OkResponse = try await client.send(
                "/mobile/api/labor/ratings/match", method: .post,
                body: MatchBody(rated: rated, rosterName: rosterName), hapticOnError: false, retryTransient: false)
            guard r.ok else { matchError = r.error ?? "Couldn't match that rating."; return }
            unmatchedRatings.removeAll { $0.rated == rated }
            Haptic.success()
            await loadTeam()
            await loadUnmatchedRatings()
        } catch let error as APIClient.APIError {
            matchError = error.message
        } catch {
            matchError = "Couldn't match that rating."
        }
    }


    /// What happened to the manager's last edit. A failed save used to be
    /// silent, which left the old score on screen beside a CHANGED badge
    /// implying it was current — on a flaky connection, the default outcome.
    enum OverrideState: Equatable { case idle, saving, failed(String) }
    var overrideState: OverrideState = .idle
    /// Bumped on every successful save. The quality panel shows its "Change
    /// saved" line for a few seconds off this; the view model used to hold
    /// a `.saved` state by sleeping four seconds inside the save, which kept
    /// Save disabled for all of them (CLIENT-62).
    var savedTick = 0
    /// The save in flight, so the next waits for it (see rescoreQuality).
    private var saveChain: Task<Void, Never>?

    private struct ReplacementsBody: Encodable {
        let rows: [ScheduleRow]
        let index: Int
    }

    private struct ReplacementsResponse: Decodable {
        let ok: Bool
        let replacements: [ScheduleReplacement]?
        let error: String?
    }

    /// Who could take this shift instead, answered by the server.
    ///
    /// This used to be decided here, and the same rule existed in three
    /// places that had drifted apart: the what-if pass checked availability,
    /// staff constraints, double booking and the forty-hour ceiling, this
    /// checked availability only, and the dashboard checked neither. One
    /// endpoint now runs the engine's own legality check for all of them.
    func loadReplacements(for row: ScheduleRow) async -> [ScheduleReplacement] {
        guard let result = scheduleResult, let rows = result.previewRows,
              let index = rows.firstIndex(where: { $0.id == row.id }) else { return [] }
        do {
            let response: ReplacementsResponse = try await client.send(
                "/mobile/api/labor/schedule/replacements", method: .post,
                body: ReplacementsBody(rows: rows, index: index), hapticOnError: false)
            return response.ok ? (response.replacements ?? []) : []
        } catch {
            return []
        }
    }

    /// Put somebody else on a shift and immediately re-score the week.
    ///
    /// No regeneration: the engine is a pure function, so this is one round
    /// trip and the manager sees what their change cost or bought before
    /// they have taken their finger off the screen.
    func overrideEmployee(rowId: String, to name: String) async {
        guard var result = scheduleResult, var rows = result.previewRows,
              let index = rows.firstIndex(where: { $0.id == rowId }) else { return }
        rows[index].employee = name
        result.previewRows = rows
        scheduleResult = result
        overriddenRows.insert(rows[index].id)
        Haptic.light()
        await rescoreQuality()
        await refreshEditCost()
    }

    /// Re-score AND store whatever is currently on screen.
    ///
    /// Storing is the whole point of an override. Without it the edit lived
    /// in this view model, the score moved, and publishing read the CSV
    /// saved at generation time — so staff received the week the manager
    /// had just fixed, unfixed, with nothing on screen to say so.
    ///
    /// Saves run one at a time. Each names the version it builds on, and two
    /// quick edits used to read `latestVersion` before either had answered —
    /// both named version 3, and the server refused the second as somebody
    /// else's save (CLIENT-29). Waiting for the one before means the second
    /// names the version the first just wrote.
    func rescoreQuality(save: Bool = true) async {
        let previous = saveChain
        let mine = Task { @MainActor [weak self] in
            await previous?.value
            await self?.performRescore(save: save)
        }
        saveChain = mine
        await mine.value
    }

    private func performRescore(save: Bool) async {
        guard var result = scheduleResult, let rows = result.previewRows, !rows.isEmpty else { return }
        isRescoringQuality = true
        overrideState = .saving
        saveNotice = nil
        defer { isRescoringQuality = false }
        // A save names the version it started from. Fetched fresh when
        // none is known yet (a week restored from the cache, or an older
        // backend that never sent versions).
        if save, latestVersion == nil, result.historyId != nil { await loadLatestVersion() }
        do {
            let response: ScoreResponse = try await client.send(
                "/mobile/api/labor/schedule/score", method: .post,
                body: ScoreBody(rows: rows, dailyTargetHours: [:], save: save, historyId: result.historyId,
                                version: save ? latestVersion : nil),
                hapticOnError: false, retryTransient: true)
            guard response.ok, let quality = response.quality else {
                overrideState = .failed(response.error ?? "Couldn't save that change.")
                return
            }
            scoreDelta = Self.delta(from: result.quality?.score, to: quality.score)
            result.quality = quality
            result.whatIf = response.whatIf
            // The compliance read moves with every edit. Only replaced when
            // the server sent one, so an older backend leaves the last
            // review standing rather than blanking it.
            if let review = response.review { result.review = review }
            if let violations = response.violations { result.ruleViolations = violations }
            if let pending = response.pendingTimeOff { result.pendingTimeOff = pending }
            scheduleResult = result
            cacheSchedule(result)
            if save { hasUnsavedFixes = false; optimizerUnsaved = false }
            overrideState = .idle
            if response.saved ?? false {
                // The version just written is now the latest; the next
                // save must name it or it would read as a conflict.
                if let v = latestVersion { latestVersion = v + 1 } else { await loadLatestVersion() }
                if let changed = response.changedSinceSent {
                    saveNotice = changed.isEmpty
                        ? "Saved; nobody's shifts changed."
                        : "Updated schedule sent to \(changed.joined(separator: ", "))."
                }
                Haptic.success()
                savedTick += 1
            }
        } catch let error as APIClient.APIError {
            // 409: somebody saved this week after it was opened. Show their
            // lines and offer Reload; the edit on screen is never written.
            if error.status == 409, let body = error.body,
               let conflict = try? JSONDecoder().decode(SaveConflict.self, from: body), conflict.conflict == true {
                saveConflict = conflict
                overrideState = .failed(conflict.error ?? "Somebody saved this week after you opened it.")
                Haptic.error()
                return
            }
            overrideState = .failed(error.message)
        } catch {
            overrideState = .failed("Couldn't save that change.")
        }
    }

    // MARK: - Operational Score

    private struct TeamResponse: Decodable {
        let ok: Bool
        let isLive: Bool?
        let team: [RatedEmployee]?
        let coverage: RatingCoverage?
        let thresholds: [String: Double]?
        let leaderRules: [ShiftLeaderRule]?
        let note: String?
        let error: String?

        enum CodingKeys: String, CodingKey {
            case ok, team, coverage, thresholds, note, error
            case isLive = "is_live"
            case leaderRules = "leader_rules"
        }
    }

    private struct RatingBody: Encodable {
        let employeeName: String
        let score: Int?
        let notes: String?
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case score, notes
        }
    }

    private struct CloserBody: Encodable {
        let employeeName: String
        let attribute = "can_close"
        let flag: Int?
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case attribute, flag
        }
    }

    private struct ThresholdsBody: Encodable {
        let thresholds: [String: Double]
        let leaderRules: [ShiftLeaderRule]
        enum CodingKeys: String, CodingKey {
            case thresholds
            case leaderRules = "leader_rules"
        }
    }

    private struct ThresholdsResponse: Decodable {
        let ok: Bool
        let thresholds: [String: Double]?
        let warnings: [String]?
        let error: String?
    }

    func loadTeam() async {
        isLoadingTeam = true
        defer { isLoadingTeam = false }
        do {
            let response: TeamResponse = try await client.send("/mobile/api/labor/team",
                                                              hapticOnError: false)
            teamNote = response.note
            team = response.team ?? []
            teamCoverage = response.coverage
            teamThresholds = response.thresholds ?? [:]
            leaderRules = response.leaderRules ?? []
            teamError = response.ok ? nil : response.error
        } catch {
            teamError = "Couldn't load your team just now."
        }
    }

    /// Set or clear one person's Operational Score.
    ///
    /// Writes the new value into the local row before the round trip and
    /// rolls it back on failure — this control is meant to be tapped down a
    /// list of twenty people, and a spinner between each tap would make
    /// rating a team feel like filing paperwork.
    func setScore(for name: String, score: Int?) async {
        guard let index = team.firstIndex(where: { $0.name == name }) else { return }
        let previous = team[index]
        savingFor = name
        teamError = nil
        defer { savingFor = nil }

        team[index].score = score
        team[index].scoreLabel = score.flatMap { Self.scoreLabels[$0] }
        recountCoverage()

        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/labor/team/rating", method: .post,
                body: RatingBody(employeeName: name, score: score, notes: nil))
            if response.ok {
                Haptic.light()
            } else {
                team[index] = previous
                recountCoverage()
                teamError = response.error ?? "Couldn't save that rating."
            }
        } catch let error as APIClient.APIError {
            team[index] = previous
            recountCoverage()
            teamError = error.message
        } catch {
            team[index] = previous
            recountCoverage()
            teamError = "Couldn't save that rating."
        }
    }

    /// Mark somebody authorised to close, or take it back.
    ///
    /// Stored against the same capability layer as the rating but as a flag
    /// rather than a score, because being trusted to lock up is not a point
    /// on a 1-to-5 scale and should not have to be earned as one.
    func setCloser(for name: String, to on: Bool) async {
        guard let index = team.firstIndex(where: { $0.name == name }) else { return }
        let previous = team[index].canClose
        savingFor = name
        teamError = nil
        defer { savingFor = nil }
        team[index].canClose = on
        do {
            let response: OkResponse = try await client.send(
                "/mobile/api/labor/team/rating", method: .post,
                body: CloserBody(employeeName: name, flag: on ? 1 : nil))
            if response.ok {
                Haptic.light()
            } else {
                team[index].canClose = previous
                teamError = response.error ?? "Couldn't save that."
            }
        } catch {
            team[index].canClose = previous
            teamError = "Couldn't save that."
        }
    }

    /// Minimum combined score per role, plus the shift leader rules.
    /// Unreachable targets come back as warnings, not errors — the save
    /// still lands, because an owner may be describing the team they mean
    /// to hire rather than the one they have.
    func saveTargets(_ thresholds: [String: Double], leaderRules rules: [ShiftLeaderRule]) async {
        isSavingTargets = true
        teamError = nil
        defer { isSavingTargets = false }
        do {
            let response: ThresholdsResponse = try await client.send(
                "/mobile/api/labor/team/thresholds", method: .post,
                body: ThresholdsBody(thresholds: thresholds, leaderRules: rules))
            if response.ok {
                teamThresholds = response.thresholds ?? thresholds
                leaderRules = rules
                targetWarnings = response.warnings ?? []
                Haptic.success()
            } else {
                teamError = response.error ?? "Couldn't save those targets."
            }
        } catch let error as APIClient.APIError {
            teamError = error.message
        } catch {
            teamError = "Couldn't save those targets."
        }
    }

    /// Coverage is recomputed locally after an optimistic rating change so
    /// the "3 of 8 rated" line moves with the tap instead of lagging a
    /// round trip behind it.
    private func recountCoverage() {
        let rated = team.filter { $0.score != nil }
        teamCoverage = RatingCoverage(
            rated: rated.count, total: team.count,
            unrated: team.filter { $0.score == nil }.map(\.name).sorted(),
            active: !rated.isEmpty,
            pct: team.isEmpty ? 0 : Int((Double(rated.count) / Double(team.count) * 100).rounded()))
    }

    /// Mirrors models.SCORE_LABELS. Duplicated rather than read from the
    /// payload so an optimistic row has a label the instant it is tapped.
    static let scoreLabels: [Int: String] = [
        1: "Very weak", 2: "Below average", 3: "Average", 4: "Strong", 5: "Excellent",
    ]

    /// Distinct roles across the roster, for the targets editor.
    var teamRoles: [String] {
        Array(Set(team.compactMap { role in
            let r = (role.role ?? "").trimmingCharacters(in: .whitespaces)
            return r.isEmpty ? nil : r
        })).sorted()
    }

    private struct GenerateResponse: Decodable {
        let ok: Bool
        let jobId: String?
        let error: String?
        // True when a generation was already running for this restaurant
        // (the lock) and the server handed back that job instead of
        // starting another. Poll it; it is not a failure.
        let joined: Bool?

        enum CodingKeys: String, CodingKey {
            case ok, error, joined
            case jobId = "job_id"
        }
    }

    private struct GenerateBody: Encodable {
        let weekStart: String?
        let dates: [String]?
        let historyId: Int?
        enum CodingKeys: String, CodingKey {
            case dates
            case weekStart = "week_start"
            case historyId = "history_id"
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encodeIfPresent(weekStart, forKey: .weekStart)
            try c.encodeIfPresent(dates, forKey: .dates)
            try c.encodeIfPresent(historyId, forKey: .historyId)
        }
    }

    /// Which week the next generation is for. Any date inside the wanted
    /// week is enough; the server snaps it to the week's start.
    enum GenerateWeek: Equatable {
        case next
        case weekAfter
        case date(Date)

        var label: String {
            switch self {
            case .next: return "Next week"
            case .weekAfter: return "The week after"
            case .date(let d): return CavnarDate.mdy(LaborViewModel.isoDay.string(from: d))
            }
        }

        /// The ISO date sent as `week_start`; nil for the server's default.
        var weekStart: String? {
            switch self {
            case .next: return nil
            case .weekAfter:
                let d = Calendar.current.date(byAdding: .day, value: 14, to: Calendar.current.startOfDay(for: Date())) ?? Date()
                return LaborViewModel.isoDay.string(from: d)
            case .date(let d): return LaborViewModel.isoDay.string(from: d)
            }
        }
    }

    static let isoDay: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = Calendar.current.timeZone
        return f
    }()

    var generateWeek: GenerateWeek = .next
    // What the running generation is redoing, for the progress copy.
    var regeneratingDates: [String] = []

    // Set when the start call joined a run already in progress — from the
    // web, or a second phone — so the progress copy says so instead of
    // pretending this tap started it.
    var joinedRunningGeneration = false

    /// Starts the same async AI schedule generation the web Labor tab uses,
    /// then polls until it completes — matches the backend's existing
    /// job-id + poll pattern (client_api.py's generate-schedule/schedule-status).
    ///
    /// `dates` + `historyId` regenerate only those days of the draft on
    /// screen; the rest are kept. Otherwise the week picker decides.
    func generateSchedule(dates: [String]? = nil, historyId: Int? = nil) async {
        let redo = (dates ?? []).isEmpty ? nil : dates
        isGeneratingSchedule = true
        scheduleError = nil
        regeneratingDates = redo ?? []
        // A partial redo keeps the week on screen until the new one lands.
        if redo == nil { scheduleResult = nil }
        joinedRunningGeneration = false
        hasUnsavedFixes = false
        overriddenRows = []
        saveConflict = nil
        saveNotice = nil
        editCost = nil
        selectedRedoDates = []
        do {
            let response: GenerateResponse = try await client.send(
                "/mobile/api/labor/generate-schedule", method: .post,
                body: GenerateBody(weekStart: redo == nil ? generateWeek.weekStart : nil,
                                   dates: redo, historyId: redo == nil ? nil : historyId)
            )
            guard response.ok, let jobId = response.jobId else {
                scheduleError = response.error ?? "Couldn't start schedule generation."
                isGeneratingSchedule = false
                regeneratingDates = []
                return
            }
            joinedRunningGeneration = response.joined ?? false
            rememberRunningGeneration(jobId, dates: redo ?? [])
            await pollSchedule(jobId: jobId)
        } catch is CancellationError {
            isGeneratingSchedule = false
            regeneratingDates = []
        } catch let error as APIClient.APIError {
            scheduleError = error.message
            isGeneratingSchedule = false
            regeneratingDates = []
        } catch {
            scheduleError = "Couldn't start schedule generation."
            isGeneratingSchedule = false
            regeneratingDates = []
        }
    }

    /// Consecutive transient failures tolerated — at the 2s interval, about
    /// a minute, which covers a deploy's restart window.
    private static let maxTransientPollFailures = 30

    private func pollSchedule(jobId: String, firstCheckWithoutWaiting: Bool = true) async {
        // ~150s max at 2s intervals. Was 30 iterations (~60s) — server logs
        // showed the real Claude call for a generation this size (full
        // shift history + YoY + weather + the longer PAR-reconciliation
        // prompt) taking ~71s end to end, so the client was giving up
        // ~8-10s before the job actually finished: it completed
        // server-side, but nothing was left polling to receive it. Wide
        // margin over the observed worst case rather than the bare minimum.
        // A 70-person week is two or three model calls and about five
        // minutes. The job runs on regardless; poll for up to 15 minutes.
        //
        // One failed check used to end the whole thing with "Lost
        // connection" while the job kept running (CLIENT-41); a transient
        // failure is now waited out. And leaving the screen is not a lost
        // connection: the sleep's cancellation ends polling quietly, with
        // the job remembered for the next visit (CLIENT-27).
        var transientFailures = 0
        func finish(error: String?) {
            scheduleError = error
            isGeneratingSchedule = false
            joinedRunningGeneration = false
            regeneratingDates = []
            forgetRunningGeneration()
        }
        for attempt in 0..<450 {
            if attempt > 0 || !firstCheckWithoutWaiting {
                do {
                    try await Task.sleep(for: .seconds(2))
                } catch {
                    return      // the screen went away; the job runs on
                }
            }
            do {
                let result: GeneratedSchedule = try await client.send(
                    "/mobile/api/labor/schedule-status/\(jobId)"
                )
                transientFailures = 0
                if result.status == "pending" { continue }
                scheduleResult = result
                finish(error: result.ok ? nil : (result.error ?? "Schedule generation failed."))
                if result.ok {
                    Haptic.success()
                    scheduleResultExpanded = true
                    cacheSchedule(result)
                    baselineRows = result.previewRows
                    editCost = nil
                    latestVersion = nil
                    scoreDelta = nil
                    optimizerUnsaved = false
                    ratedInPrompt = [:]
                    suppressedRecommendationKinds = result.quality?.suppressedRecommendationKinds ?? []
                    await loadLatestVersion()
                }
                return
            } catch is CancellationError {
                return
            } catch let error as APIClient.APIError where error.isTransientForPolling {
                transientFailures += 1
                if transientFailures >= Self.maxTransientPollFailures {
                    // Keep the job remembered: it may still finish, and the
                    // next visit picks it up.
                    scheduleError = "Lost the connection while your schedule was being built. It keeps going — come back to Labor in a minute to pick it up."
                    isGeneratingSchedule = false
                    joinedRunningGeneration = false
                    regeneratingDates = []
                    return
                }
            } catch let error as APIClient.APIError {
                finish(error: error.message)
                return
            } catch {
                finish(error: "Couldn't check on your schedule.")
                return
            }
        }
        finish(error: "Schedule generation is taking longer than expected — check back in a bit.")
    }
}
