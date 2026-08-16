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

struct LaborStats: Decodable {
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
    }
}

struct ScheduleRow: Codable, Identifiable {
    let date: String?
    let day: String?
    let employee: String?
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
    let previewRows: [ScheduleRow]?
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

    enum CodingKeys: String, CodingKey {
        case ok, status, summary, error
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

    private let client: APIClient
    private var restaurantId: Int?

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// Loads whatever schedule was last generated and cached on this device
    /// for this restaurant, if any — called before the network load() so a
    /// relaunch shows the last real result immediately instead of an empty
    /// Overview tab, rather than losing it the moment the process restarts
    /// (previously in-memory only).
    func configureCaching(restaurantId: Int) {
        self.restaurantId = restaurantId
        guard let data = UserDefaults.standard.data(forKey: Self.scheduleCacheKey(restaurantId)),
              let cached = try? Self.cacheDecoder.decode(GeneratedSchedule.self, from: data),
              !Self.isStale(cached) else { return }
        scheduleResult = cached
    }

    private static func scheduleCacheKey(_ restaurantId: Int) -> String { "labor.cachedSchedule.\(restaurantId)" }

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
        UserDefaults.standard.set(data, forKey: Self.scheduleCacheKey(restaurantId))
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            stats = try await client.send("/mobile/api/labor")
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
