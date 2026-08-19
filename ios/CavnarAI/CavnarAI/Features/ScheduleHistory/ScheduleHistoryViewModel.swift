import Foundation
import Observation

struct ScheduleHistoryEntry: Codable, Identifiable {
    let id: Int
    let generatedAt: String
    let weekStart: String?
    let weekEnd: String?
    let hoursScheduled: Double?
    let hoursBudget: Double?
    let laborTarget: Double?

    enum CodingKeys: String, CodingKey {
        case id
        case generatedAt = "generated_at"
        case weekStart = "week_start"
        case weekEnd = "week_end"
        case hoursScheduled = "hours_scheduled"
        case hoursBudget = "hours_budget"
        case laborTarget = "labor_target"
    }
}

@Observable
@MainActor
final class ScheduleHistoryViewModel {
    var history: [ScheduleHistoryEntry] = []
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient
    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct Response: Decodable {
        let ok: Bool
        let history: [ScheduleHistoryEntry]
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: Response = try await client.send("/mobile/api/labor/schedule-history")
            history = response.history
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load schedule history."
        }
    }
}

/// Detail reuses GeneratedSchedule (defined in LaborViewModel.swift) —
/// the history detail endpoint returns the same shape (schedule_csv,
/// summary, preview_rows, hours_scheduled/budget) plus a few extra fields
/// (id, generated_at, week_start/end) that Decodable simply ignores.
@Observable
@MainActor
final class ScheduleHistoryDetailViewModel {
    var detail: GeneratedSchedule?
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient
    init(client: APIClient = .shared) {
        self.client = client
    }

    func load(id: Int) async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            detail = try await client.send("/mobile/api/labor/schedule-history/\(id)")
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load this schedule."
        }
    }
}
