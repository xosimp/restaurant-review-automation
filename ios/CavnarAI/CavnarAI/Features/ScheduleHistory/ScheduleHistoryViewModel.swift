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
    // Deliberately separate from errorMessage above — that one gates
    // whether the List renders at all (see ScheduleHistoryView.body),
    // so a failed delete setting the SAME property was replacing the
    // entire list with a full-screen error the moment one swipe-delete
    // failed, rather than surfacing just that one failure.
    var deleteErrorMessage: String?

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

    private struct OkResponse: Decodable {
        let ok: Bool
        let error: String?
    }

    /// The only way a schedule ever leaves this list — nothing here or
    /// server-side removes one automatically (see delete_schedule_history's
    /// own doc comment). Removes locally on success rather than a full
    /// reload, so swipe-to-delete's row-removal animation stays smooth.
    func delete(id: Int) async {
        do {
            let _: OkResponse = try await client.send(
                "/mobile/api/labor/schedule-history/\(id)", method: .delete
            )
            history.removeAll { $0.id == id }
            Haptic.selection()
        } catch let error as APIClient.APIError {
            // The real underlying message (e.g. a specific status code),
            // not a hardcoded string — needed to actually diagnose why a
            // delete is failing rather than guessing at it blind.
            deleteErrorMessage = error.message
        } catch {
            deleteErrorMessage = "Couldn't delete that schedule."
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

    // Tracks which id is currently loaded/loading so a second .task/
    // onAppear firing for the same id (see ScheduleHistoryDetailView's
    // belt-and-suspenders comment) is a harmless no-op instead of
    // re-fetching or racing the first call.
    private var loadedID: Int?

    func load(id: Int) async {
        guard loadedID != id || (detail == nil && errorMessage == nil) else { return }
        loadedID = id
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            // Explicit non-optional local, not a direct assignment into
            // the optional `detail` property — matches every other
            // load() in this codebase (see LaborViewModel.pollSchedule).
            // Assigning straight into an Optional-typed property lets the
            // compiler infer send<Response>'s Response as GeneratedSchedule?
            // instead of GeneratedSchedule, decoding the JSON as a
            // top-level Optional rather than the concrete type.
            let fetched: GeneratedSchedule = try await client.send("/mobile/api/labor/schedule-history/\(id)")
            detail = fetched
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load this schedule."
        }
    }
}
