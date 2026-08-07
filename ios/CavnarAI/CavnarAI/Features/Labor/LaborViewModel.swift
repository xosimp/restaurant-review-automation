import Foundation
import Observation

struct LaborOvertimeEntry: Codable, Identifiable {
    let employee: String?
    let hours: Double?
    let week: String?
    let status: String?
    var id: String { "\(employee ?? "")-\(week ?? "")" }
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

struct LaborStats: Decodable {
    let ok: Bool
    let isLive: Bool
    let overallLaborPct: Double
    let target: Double
    let onTrack: Bool
    let potentialSavings: Double
    let overtimeRisk: [LaborOvertimeEntry]
    let roleSummary: [LaborRoleSummary]

    enum CodingKeys: String, CodingKey {
        case ok
        case isLive = "is_live"
        case overallLaborPct = "overall_labor_pct"
        case target
        case onTrack = "on_track"
        case potentialSavings = "potential_savings"
        case overtimeRisk = "overtime_risk"
        case roleSummary = "role_summary"
    }
}

struct ScheduleRow: Decodable, Identifiable {
    let date: String?
    let day: String?
    let employee: String?
    let role: String?
    let shiftStart: String?
    let shiftEnd: String?
    let scheduledHours: String?
    let notes: String?

    var id: String { "\(date ?? "")-\(employee ?? "")-\(shiftStart ?? "")" }

    enum CodingKeys: String, CodingKey {
        case date, day, employee, role, notes
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case scheduledHours = "scheduled_hours"
    }
}

struct GeneratedSchedule: Decodable {
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

    enum CodingKeys: String, CodingKey {
        case ok, status, summary, error
        case weekDates = "week_dates"
        case weekDays = "week_days"
        case hoursScheduled = "hours_scheduled"
        case laborTarget = "labor_target"
        case previewRows = "preview_rows"
        case scheduleCsv = "schedule_csv"
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

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
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
        for _ in 0..<30 {  // ~60s max at 2s intervals
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
