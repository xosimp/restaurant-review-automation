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
    /// The same lines as `recommendations`, each with its rec_ledger key and
    /// the competitor reviews it rests on. Answered lines are already gone
    /// from both. Optional: an older server sends only the strings.
    let recommendationItems: [IntelRecommendation]?
    /// How many recommendations were held back because the read carried an
    /// unverified figure or business, and what it was.
    let recommendationsWithheld: Int?
    let recommendationsUnverified: String?
    /// True when the read said there is nothing worth acting on.
    let nothingToActOn: Bool?
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
    /// I10: where the owner stands against the market — one rule for every
    /// surface (competitor_intel_format.market_standing): the gap in stars,
    /// "ahead" | "level" | "behind" | nil, the words and the tone. Nil
    /// (and "neutral") when the two ratings are not the same kind of number.
    /// Absent on an older server, which keeps the client's own colouring.
    var ownVsMarket: Double? = nil
    var standing: String? = nil
    var standingLabel: String? = nil
    var standingTone: ServerTone? = nil
    /// Benchmarking #38: how many matched rivals and how far out the
    /// standing rests on, and the tie band ("3 restaurants matched on
    /// cuisine and price within 2.4 km · a gap inside ±0.1★ reads as
    /// level"); or why there is no standing yet.
    var standingBasis: String? = nil
    var standingWhyNot: String? = nil

    /// "You lead the block · +0.3★ against the market" — nil when the
    /// server made no comparison.
    var standingLine: String? {
        guard standing != nil, let label = standingLabel?.trimmingCharacters(in: .whitespaces), !label.isEmpty
        else { return nil }
        guard let gap = ownVsMarket else { return label }
        let signed = (gap > 0 ? "+" : gap < 0 ? "\u{2212}" : "\u{00B1}") + String(format: "%.1f", abs(gap))
        return "\(label) \u{00B7} \(signed)\u{2605} against the market"
    }

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
        case recommendationItems = "recommendation_items"
        case recommendationsWithheld = "recommendations_withheld"
        case recommendationsUnverified = "recommendations_unverified"
        case nothingToActOn = "nothing_to_act_on"
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
        case ownVsMarket = "own_vs_market"
        case standing
        case standingLabel = "standing_label"
        case standingTone = "standing_tone"
        case standingBasis = "standing_basis"
        case standingWhyNot = "standing_why_not"
        case stale
        case ageDays = "age_days"
        case asOf = "as_of"
    }

    /// What the recommendations block renders: the keyed items when the
    /// server sent them, otherwise the plain strings with no controls.
    var displayRecommendations: [IntelRecommendation] {
        if let items = recommendationItems { return items }
        return recommendations.map { IntelRecommendation(key: nil, text: $0, cites: nil) }
    }

    /// The sentence for an empty recommendations list, or nil when the list
    /// isn't empty or there's nothing to explain.
    var emptyRecommendationsNote: String? {
        guard displayRecommendations.isEmpty else { return nil }
        if let n = recommendationsWithheld, n > 0 {
            let why = (recommendationsUnverified?.isEmpty == false) ? recommendationsUnverified! : "couldn\u{2019}t be verified"
            return "\(n) recommendation\(n == 1 ? "" : "s") held back: this read \(why)."
        }
        if nothingToActOn == true { return "Nothing worth acting on this week." }
        return nil
    }
}

/// One competitive recommendation and the competitor reviews it cites.
struct IntelRecommendation: Decodable, Identifiable, Equatable {
    /// Nil only for a line built from an older server's plain string.
    let key: String?
    let text: String
    let cites: [Cite]?

    var id: String { key ?? text }

    /// A competitor review the recommendation rests on (its R-number ref,
    /// whose it is, the stars, Google's relative time and the text).
    struct Cite: Decodable, Identifiable, Equatable {
        let ref: String?
        let competitor: String?
        let rating: Double?
        let time: String?
        let text: String?
        var id: String { (ref ?? "") + (competitor ?? "") + (text ?? "") }

        enum CodingKeys: String, CodingKey { case ref, competitor, rating, time, text }

        /// Field by field and forgiving: a cite is supporting detail, and
        /// one odd value must not fail the whole Intel payload.
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ref = try? c.decodeIfPresent(String.self, forKey: .ref)
            competitor = try? c.decodeIfPresent(String.self, forKey: .competitor)
            rating = try? c.decodeIfPresent(Double.self, forKey: .rating)
            time = try? c.decodeIfPresent(String.self, forKey: .time)
            text = try? c.decodeIfPresent(String.self, forKey: .text)
        }

        init(ref: String?, competitor: String?, rating: Double?, time: String?, text: String?) {
            self.ref = ref
            self.competitor = competitor
            self.rating = rating
            self.time = time
            self.text = text
        }
    }
}

/// Competitor data itself is read-only from a normal load — refreshing it
/// (Google Places + Claude generation, 20-40s) is its own async job, the
/// same job-id/poll pattern LaborViewModel.generateSchedule already uses
/// for schedule generation (see mobile_api.py's intel/refresh-competitors
/// + intel/refresh-status/<job_id>).
/// GET /mobile/api/intel/movement — who joined or left the competitor set
/// between the last two weekly checks, and the rating moves past what the
/// review volume could produce by chance (models.competitor_movement). The
/// snapshots were written every week and shown nowhere.
struct IntelMovement: Decodable, Equatable {
    struct Place: Decodable, Equatable, Identifiable {
        let placeId: String?
        let name: String
        var id: String { (placeId ?? "") + name }
        enum CodingKeys: String, CodingKey { case name; case placeId = "place_id" }
    }
    struct Move: Decodable, Equatable, Identifiable {
        let placeId: String?
        let name: String
        let ratingThen: Double
        let ratingNow: Double
        let ratingChange: Double
        let reviewsAdded: Int?
        var id: String { (placeId ?? "") + name }
        enum CodingKeys: String, CodingKey {
            case name
            case placeId = "place_id"
            case ratingThen = "rating_then"
            case ratingNow = "rating_now"
            case ratingChange = "rating_change"
            case reviewsAdded = "reviews_added"
        }
    }
    let ok: Bool
    let days: Int?
    let significant: [Move]
    let arrived: [Place]
    let gone: [Place]
    /// The earlier of the two weekly checks compared (ISO date); nil until
    /// there are two to compare.
    let comparedFrom: String?

    enum CodingKeys: String, CodingKey {
        case ok, days, significant, arrived, gone
        case comparedFrom = "compared_from"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decode(Bool.self, forKey: .ok)
        days = try c.decodeIfPresent(Int.self, forKey: .days)
        significant = (try? c.decodeIfPresent([Move].self, forKey: .significant)) ?? []
        arrived = (try? c.decodeIfPresent([Place].self, forKey: .arrived)) ?? []
        gone = (try? c.decodeIfPresent([Place].self, forKey: .gone)) ?? []
        comparedFrom = try c.decodeIfPresent(String.self, forKey: .comparedFrom)
    }

    var hasChanges: Bool { !arrived.isEmpty || !gone.isEmpty || !significant.isEmpty }
}

@Observable
@MainActor
final class IntelViewModel {
    var summary: IntelSummary?
    /// Nil until loaded, or when the route failed — the section then stays
    /// hidden rather than claiming nothing changed.
    var movement: IntelMovement?
    var isLoading = false
    var errorMessage: String?

    var isRefreshing = false
    var refreshError: String?
    /// When /mobile/api/intel last answered — the foreground-refresh clock.
    private(set) var lastLoadedAt: Date?

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
            lastLoadedAt = Date()
            await loadMovement()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
        } catch {
            errorMessage = "Couldn't load competitor intel."
        }
    }

    func loadMovement() async {
        let m: IntelMovement? = try? await client.send("/mobile/api/intel/movement", hapticOnError: false)
        movement = (m?.ok == true) ? m : nil
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
    //
    // A transient failure (a deploy's 502, a dropped connection) is waited
    // out rather than ending the refresh while the job runs on (CLIENT-41);
    // leaving the screen ends polling quietly.
    private func pollRefresh(jobId: String) async {
        for attempt in 0..<30 {
            if attempt > 0 {
                do {
                    try await Task.sleep(for: .seconds(2))
                } catch {
                    isRefreshing = false
                    return
                }
            }
            do {
                let result: RefreshStatusResponse = try await client.send(
                    "/mobile/api/intel/refresh-status/\(jobId)"
                )
                if result.status == "pending" { continue }
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
            } catch is CancellationError {
                isRefreshing = false
                return
            } catch let error as APIClient.APIError where error.isTransientForPolling {
                continue
            } catch let error as APIClient.APIError {
                refreshError = error.message
                isRefreshing = false
                return
            } catch {
                refreshError = "Couldn't check on the competitor refresh."
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
