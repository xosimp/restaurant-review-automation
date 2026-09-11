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
    let updatedBy: String?
    let updatedAt: String?
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, role, shifts, score, notes
        case scoreLabel = "score_label"
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

    var scoredShifts: [QualityShift] { (shifts ?? []).filter { $0.scored } }
    var customerDimensions: [QualityDimension] {
        (dimensions ?? []).filter { $0.isCustomerFacing }
    }

    enum CodingKeys: String, CodingKey {
        case checked, score, band, shifts, dimensions, strengths, weaknesses
        case recommendations, confidence, best, worst, reason
        case belowProfile = "below_profile"
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
    let needsReview: Bool?

    var id: String { "\(date ?? "")-\(employee ?? "")-\(shiftStart ?? "")" }

    enum CodingKeys: String, CodingKey {
        case date, day, employee, role, notes
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case scheduledHours = "scheduled_hours"
        case needsReview = "needs_review"
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

    enum CodingKeys: String, CodingKey {
        case ok, status, summary, error, strength, quality
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
        } catch {
            errorMessage = "Couldn't load labor stats."
        }
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

    private struct OkResponse: Decodable {
        let ok: Bool
        let error: String?
    }

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
        enum CodingKeys: String, CodingKey {
            case rows
            case dailyTargetHours = "daily_target_hours"
        }
    }

    private struct ScoreResponse: Decodable {
        let ok: Bool
        let quality: ScheduleQuality?
        let whatIf: ScheduleWhatIf?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, quality, error
            case whatIf = "what_if"
        }
    }

    /// Who could take this shift instead: same role, not already working
    /// that day, and not somebody who said they cannot work it.
    ///
    /// Ordered strongest first, because the reason a manager opens this is
    /// almost always a shift the engine just told them is weak.
    func replacements(for row: ScheduleRow) -> [RatedEmployee] {
        guard let result = scheduleResult, let rows = result.previewRows else { return [] }
        let role = (row.role ?? "").trimmingCharacters(in: .whitespaces).lowercased()
        let date = row.date ?? ""
        let day = row.day ?? ""
        let working = Set(rows.filter { $0.date == date }
                              .compactMap { $0.employee?.lowercased() })
        let blocked = Dictionary(uniqueKeysWithValues: availability.map {
            ($0.employeeName.lowercased(), Set($0.unavailableDays))
        })
        return team
            .filter { member in
                guard member.name.lowercased() != (row.employee ?? "").lowercased() else { return false }
                guard !working.contains(member.name.lowercased()) else { return false }
                if blocked[member.name.lowercased()]?.contains(day) == true { return false }
                let memberRole = (member.role ?? "").trimmingCharacters(in: .whitespaces).lowercased()
                return role.isEmpty || memberRole.isEmpty || memberRole == role
            }
            .sorted { ($0.score ?? 0, $0.name) > ($1.score ?? 0, $1.name) }
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
    }

    /// Re-score whatever is currently on screen.
    func rescoreQuality() async {
        guard var result = scheduleResult, let rows = result.previewRows, !rows.isEmpty else { return }
        isRescoringQuality = true
        defer { isRescoringQuality = false }
        do {
            let response: ScoreResponse = try await client.send(
                "/mobile/api/labor/schedule/score", method: .post,
                body: ScoreBody(rows: rows, dailyTargetHours: [:]),
                hapticOnError: false, retryTransient: true)
            guard response.ok, let quality = response.quality else { return }
            result.quality = quality
            result.whatIf = response.whatIf
            scheduleResult = result
            cacheSchedule(result)
        } catch {
            // The edit itself stands; only the score is stale. Saying
            // "couldn't re-score" over a schedule the manager just fixed
            // would read as the edit having failed.
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

        enum CodingKeys: String, CodingKey {
            case ok, error
            case jobId = "job_id"
        }
    }

    /// Starts the same async AI schedule generation the web Labor tab uses,
    /// then polls until it completes — matches the backend's existing
    /// job-id + poll pattern (client_api.py's generate-schedule/schedule-status).
    func generateSchedule() async {
        isGeneratingSchedule = true
        scheduleError = nil
        scheduleResult = nil
        do {
            let response: GenerateResponse = try await client.send(
                "/mobile/api/labor/generate-schedule", method: .post
            )
            guard response.ok, let jobId = response.jobId else {
                scheduleError = response.error ?? "Couldn't start schedule generation."
                isGeneratingSchedule = false
                return
            }
            await pollSchedule(jobId: jobId)
        } catch let error as APIClient.APIError {
            scheduleError = error.message
            isGeneratingSchedule = false
        } catch {
            scheduleError = "Couldn't start schedule generation."
            isGeneratingSchedule = false
        }
    }

    private func pollSchedule(jobId: String) async {
        // ~150s max at 2s intervals. Was 30 iterations (~60s) — server logs
        // showed the real Claude call for a generation this size (full
        // shift history + YoY + weather + the longer PAR-reconciliation
        // prompt) taking ~71s end to end, so the client was giving up
        // ~8-10s before the job actually finished: it completed
        // server-side, but nothing was left polling to receive it. Wide
        // margin over the observed worst case rather than the bare minimum.
        for _ in 0..<75 {
            do {
                let result: GeneratedSchedule = try await client.send(
                    "/mobile/api/labor/schedule-status/\(jobId)"
                )
                if result.status == "pending" {
                    try? await Task.sleep(for: .seconds(2))
                    continue
                }
                scheduleResult = result
                isGeneratingSchedule = false
                if !result.ok {
                    scheduleError = result.error ?? "Schedule generation failed."
                } else {
                    Haptic.success()
                    scheduleResultExpanded = true
                    cacheSchedule(result)
                }
                return
            } catch let error as APIClient.APIError {
                scheduleError = error.message
                isGeneratingSchedule = false
                return
            } catch {
                scheduleError = "Lost connection while generating your schedule."
                isGeneratingSchedule = false
                return
            }
        }
        scheduleError = "Schedule generation is taking longer than expected — check back in a bit."
        isGeneratingSchedule = false
    }
}
