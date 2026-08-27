import Foundation
import Observation

struct IntelSection: Codable, Identifiable {
    let name: String
    let bullets: [String]

    var id: String { name }
}

struct CompetitorReview: Decodable, Identifiable {
    let author: String
    let rating: Int
    let text: String
    let time: String

    var id: String { author + text }
}

struct Competitor: Decodable, Identifiable {
    let name: String
    let rating: Double
    let reviewCount: Int
    let vicinity: String
    let reviews: [CompetitorReview]
    let placeId: String
    // True for a competitor the owner added themselves (custom_competitors
    // on the backend) rather than one Google's own nearby-search surfaced —
    // drives the delete affordance in competitorRow, which only makes
    // sense for something the owner actually chose to add.
    let isCustom: Bool

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, rating, vicinity, reviews
        case reviewCount = "review_count"
        case placeId = "place_id"
        case isCustom = "custom"
    }
}

struct PlaceSearchResult: Decodable, Identifiable {
    let placeId: String
    let name: String
    let address: String
    let rating: Double
    let reviewCount: Int

    var id: String { placeId }

    enum CodingKeys: String, CodingKey {
        case placeId = "place_id"
        case name, address, rating
        case reviewCount = "review_count"
    }
}

struct IntelSummary: Decodable {
    let ok: Bool
    let hasData: Bool
    let restaurantName: String?
    let ownerName: String?
    let intro: String?
    let recommendations: [String]
    let sections: [IntelSection]
    let competitors: [Competitor]
    let updatedAt: String?
    let ownRating: Double?

    enum CodingKeys: String, CodingKey {
        case ok, intro, recommendations, sections, competitors
        case hasData = "has_data"
        case restaurantName = "restaurant_name"
        case ownerName = "owner_name"
        case updatedAt = "updated_at"
        case ownRating = "own_rating"
    }
}

/// Competitor data itself is read-only from a normal load — refreshing it
/// (Google Places + Claude generation, 20-40s) is its own async job, the
/// same job-id/poll pattern LaborViewModel.generateSchedule already uses
/// for schedule generation (see mobile_api.py's intel/refresh-competitors
/// + intel/refresh-status/<job_id>).
@Observable
@MainActor
final class IntelViewModel {
    var summary: IntelSummary?
    var isLoading = false
    var errorMessage: String?

    var isRefreshing = false
    var refreshError: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            summary = try await client.send("/mobile/api/intel")
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load competitor intel."
        }
    }

    private struct RefreshStartResponse: Decodable {
        let ok: Bool
        let error: String?
        let jobId: String?

        enum CodingKeys: String, CodingKey {
            case ok, error
            case jobId = "job_id"
        }
    }

    private struct RefreshStatusResponse: Decodable {
        let ok: Bool
        let status: String
        let error: String?
    }

    func refreshCompetitors() async {
        isRefreshing = true
        refreshError = nil
        do {
            let response: RefreshStartResponse = try await client.send(
                "/mobile/api/intel/refresh-competitors", method: .post
            )
            guard response.ok, let jobId = response.jobId else {
                refreshError = response.error ?? "Couldn't start competitor refresh."
                isRefreshing = false
                return
            }
            await pollRefresh(jobId: jobId)
        } catch let error as APIClient.APIError {
            refreshError = error.message
            isRefreshing = false
        } catch {
            refreshError = "Couldn't start competitor refresh."
            isRefreshing = false
        }
    }

    // ~60s max at 2s intervals — the web route's own docstring puts the
    // real analysis (Google Places calls + Claude generation) at 20-40s.
    private func pollRefresh(jobId: String) async {
        for _ in 0..<30 {
            do {
                let result: RefreshStatusResponse = try await client.send(
                    "/mobile/api/intel/refresh-status/\(jobId)"
                )
                if result.status == "pending" {
                    try? await Task.sleep(for: .seconds(2))
                    continue
                }
                isRefreshing = false
                if !result.ok {
                    refreshError = result.error ?? "Competitor refresh failed."
                } else {
                    Haptic.success()
                    // The job's own result body is the raw generation output
                    // (unparsed narrative text), not the parsed intro/
                    // sections/recommendations shape the rest of this screen
                    // renders — reload the normal summary instead of trying
                    // to render the job body directly, same as the web
                    // dashboard's loadCompetitorIntel() re-fetch after its
                    // own refresh completes.
                    await load()
                }
                return
            } catch let error as APIClient.APIError {
                refreshError = error.message
                isRefreshing = false
                return
            } catch {
                refreshError = "Lost connection while refreshing competitor data."
                isRefreshing = false
                return
            }
        }
        refreshError = "Competitor refresh is taking longer than expected — check back in a bit."
        isRefreshing = false
    }

    private struct SearchPlacesResponse: Decodable {
        let ok: Bool
        let results: [PlaceSearchResult]?
    }

    /// hapticOnError: false — a search that comes back empty because the
    /// name doesn't exist yet mid-typing isn't a "failure" the way a login
    /// error is; this just quietly returns nothing for AddCompetitorSheet
    /// to render as "no matches" rather than buzzing an error haptic on
    /// every few keystrokes of a normal search.
    func searchPlaces(query: String) async -> [PlaceSearchResult] {
        do {
            let response: SearchPlacesResponse = try await client.send(
                "/mobile/api/intel/search-places", query: ["q": query], hapticOnError: false
            )
            return response.results ?? []
        } catch {
            return []
        }
    }

    private struct PlaceIdBody: Encodable {
        let placeId: String
        enum CodingKeys: String, CodingKey { case placeId = "place_id" }
    }

    /// Saves the Place ID, then immediately kicks off the same refresh job
    /// refreshLink/refreshButton already trigger — so the newly-added
    /// competitor's reviews get pulled in and the AI insight regenerates
    /// to reflect them without the owner needing to separately remember to
    /// hit "Refresh" right after adding one.
    @discardableResult
    func addCompetitor(placeId: String) async -> Bool {
        do {
            let _: APIClient.EmptyResponse = try await client.send(
                "/mobile/api/intel/add-competitor", method: .post, body: PlaceIdBody(placeId: placeId)
            )
            await refreshCompetitors()
            return true
        } catch {
            return false
        }
    }

    func removeCompetitor(placeId: String) async {
        do {
            let _: APIClient.EmptyResponse = try await client.send(
                "/mobile/api/intel/remove-competitor", method: .post, body: PlaceIdBody(placeId: placeId)
            )
            await refreshCompetitors()
        } catch {
            refreshError = "Couldn't remove that competitor — try again."
        }
    }
}
