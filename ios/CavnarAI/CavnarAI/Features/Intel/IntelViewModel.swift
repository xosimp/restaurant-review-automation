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
    // Which of four relaxation passes selected this one — the last widens
    // to 8km with no cuisine or price match, and that used to arrive
    // looking exactly like a direct match across the street.
    let matchBasis: String?
    let distanceM: Int?
    let priceLevel: Int?
    // A rating resting on a handful of reviews is not a reputation.
    let ratingIsProvisional: Bool?
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
        case matchBasis = "match_basis"
        case distanceM = "distance_m"
        case priceLevel = "price_level"
        case ratingIsProvisional = "rating_is_provisional"
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
    // Google's own all-time rating for this restaurant, and what it rests
    // on. This used to be the average over the reviews Cavnar AI happened
    // to import, rendered beside competitors' all-time figures and coloured
    // by the comparison.
    let ownRatingBasis: String?
    let ownRatingCount: Int?
    // The market figure, weighted by each competitor's review volume.
    let marketRating: Double?
    let marketRatingReviews: Int?
    let marketRatingN: Int?
    let claimKinds: [String: String]?

    /// True only when the owner's rating and the market figure are the same
    /// kind of number.
    var ratingsAreComparable: Bool { ownRatingBasis == "google_all_time" }

    /// Why the head-to-head cannot be read as like for like, or nil.
    var ratingComparisonCaveat: String? {
        guard ownRating != nil else { return nil }
        if ownRatingBasis == "imported_sample" {
            let n = ownRatingCount.map { " (\($0) reviews)" } ?? ""
            return "Your figure is the average of the reviews we've imported\(n), not Google's all-time rating. Connect Google Business Profile in Account to compare like for like."
        }
        return nil
    }
    // ai_guard.freshness has been computed on this payload all along, with a
    // docstring saying a summary written six weeks ago read exactly like one
    // written this morning. The date was decoded; the judgement about it was
    // not, so nothing ever said the intelligence was old.
    let stale: Bool?
    let ageDays: Int?
    let asOf: String?
    let ownRating: Double?

    /// Set when this analysis is old enough that it should not be read as
    /// current. Nil when it is fresh.
    var stalenessNote: String? {
        guard stale == true else { return nil }
        if let d = ageDays {
            return "This snapshot is \(d) days old. Competitor ratings and complaints move; treat it as background, not as today's picture."
        }
        return "This snapshot is out of date. Treat it as background, not as today's picture."
    }

    enum CodingKeys: String, CodingKey {
        case ok, intro, recommendations, sections, competitors
        case hasData = "has_data"
        case restaurantName = "restaurant_name"
        case ownerName = "owner_name"
        case updatedAt = "updated_at"
        case ownRating = "own_rating"
        case ownRatingBasis = "own_rating_basis"
        case ownRatingCount = "own_rating_count"
        case marketRating = "market_rating"
        case marketRatingReviews = "market_rating_reviews"
        case marketRatingN = "market_rating_n"
        case claimKinds = "claim_kinds"
        case stale
        case ageDays = "age_days"
        case asOf = "as_of"
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

    /// load(), not refreshCompetitors() — the backend route now drops the
    /// competitor from the already-cached analysis blob directly (see
    /// remove_competitor_from_cache in competitor.py), so a plain re-fetch
    /// of /intel already reflects the removal. Calling refreshCompetitors()
    /// here used to mean every deletion waited on the SAME 20-40s Google
    /// Places + Claude job add-competitor genuinely needs — a removal
    /// never needed fresh data at all, just a smaller list, which is why
    /// it visibly lagged for several seconds for no real reason.
    func removeCompetitor(placeId: String) async {
        do {
            let _: APIClient.EmptyResponse = try await client.send(
                "/mobile/api/intel/remove-competitor", method: .post, body: PlaceIdBody(placeId: placeId)
            )
            await load()
        } catch {
            refreshError = "Couldn't remove that competitor — try again."
        }
    }
}
