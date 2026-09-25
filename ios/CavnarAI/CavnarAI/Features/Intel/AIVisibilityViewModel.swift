import Foundation
import Observation

struct AIVisibilityQuery: Decodable, Identifiable {
    let query: String
    let answer: String
    let appeared: Bool
    // What kind of question this is. A branded "tell me about X" is not
    // evidence that an open search would surface you, and blending the two
    // into one number answered neither question.
    let kind: String?
    // The pages the answer was grounded in. Fetched on every run since the
    // prompt stopped suppressing citations, returned in the payload, and
    // decoded by nothing — so every answer on screen was exactly as
    // unverifiable as before that fix.
    let sources: [String]?
    // Competitors of this restaurant that the same answer named.
    let competitorsNamed: [String]?

    var id: String { query }

    enum CodingKeys: String, CodingKey {
        case query, answer, appeared, kind, sources
        case competitorsNamed = "competitors_named"
    }
}

struct CompetitorAppearance: Decodable, Identifiable {
    let name: String
    let queries: Int
    let share: Int
    var id: String { name }
}

struct AIVisibilityRun: Decodable, Identifiable {
    let aiScore: Int
    let gbpScore: Int?
    let answered: Int?
    let appeared: Int?
    let createdAt: String
    var id: String { createdAt }
    enum CodingKeys: String, CodingKey {
        case aiScore = "ai_score"
        case gbpScore = "gbp_score"
        // Sample size behind this run, so a change can be told from a
        // difference in how many questions came back.
        case answered
        case appeared
        case createdAt = "created_at"
    }
}

struct AIVisibilityChecklistItem: Decodable, Identifiable {
    let label: String
    let done: Bool
    let pts: Int
    let action: String
    let needsGmb: Bool

    var id: String { label }

    enum CodingKeys: String, CodingKey {
        case label, done, pts, action
        case needsGmb = "needs_gmb"
    }
}

/// One card of the server-built AI-visibility roadmap
/// (`client_api.ai_visibility_roadmap`): the same four cards, order and done
/// rules as the web, each keyed (`aiv_roadmap:<kind>`) and presented on
/// `intel` while open — so the phone answers the same recommendation the
/// web does instead of drawing its own copy of it (rec-ROI #41).
struct AIVisibilityRoadmapCard: Decodable, Identifiable, Equatable {
    let key: String
    let recKey: String?
    let title: String
    let why: String?
    let detail: String?
    let action: String?
    let impact: String?
    let module: String?
    let done: Bool
    let answered: Bool?
    let answerable: Bool?
    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, title, why, detail, action, impact, module, done, answered, answerable
        case recKey = "rec_key"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        recKey = try? c.decodeIfPresent(String.self, forKey: .recKey)
        title = try c.decode(String.self, forKey: .title)
        why = try? c.decodeIfPresent(String.self, forKey: .why)
        detail = try? c.decodeIfPresent(String.self, forKey: .detail)
        action = try? c.decodeIfPresent(String.self, forKey: .action)
        impact = try? c.decodeIfPresent(String.self, forKey: .impact)
        module = try? c.decodeIfPresent(String.self, forKey: .module)
        done = (try? c.decodeIfPresent(Bool.self, forKey: .done)) ?? false
        answered = try? c.decodeIfPresent(Bool.self, forKey: .answered)
        answerable = try? c.decodeIfPresent(Bool.self, forKey: .answerable)
    }

    /// Done / Not for us under an open card the owner hasn't answered.
    var showsAnswers: Bool { !done && answerable == true && answered != true }
}

struct AIVisibilityResult: Decodable {
    let ok: Bool
    /// The server's roadmap (newer servers). Nil on an older one, which
    /// keeps the locally built cards.
    var roadmap: [AIVisibilityRoadmapCard]? = nil
    let restaurantName: String?
    let queries: [AIVisibilityQuery]?
    let appearedCount: Int?
    let totalQueries: Int?
    let aiScore: Int?
    // The honest bounds on aiScore for this sample. A handful of questions
    // answered by a non-deterministic model is a range, not a point.
    let aiScoreLow: Int?
    let aiScoreHigh: Int?
    // How many of the questions we sent actually came back. When this is
    // below totalQueries the run is partial and the score is an estimate.
    let answeredQueries: Int?
    let partial: Bool?
    // False when we have no city for this restaurant, which makes two
    // locations of one brand indistinguishable in an answer — appearance
    // cannot be judged at all, and a zero would be a statement about the
    // profile rather than about the restaurant.
    let locationKnown: Bool?
    let city: String?
    let citySource: String?
    let gbpScore: Int?
    // gbpScore's two halves, split apart. presenceScore covers the
    // restaurant's actual public listing and review record; setup counts
    // this product's own configuration and is deliberately not scored.
    let presenceScore: Int?
    /// How many presence items the score is out of, and how many could not
    /// be read (left out of it, never counted as 0) — M-14.
    let presenceMeasured: Int?
    let presenceUnmeasured: Int?
    /// The server's tone for the listing-strength figure (I10) — used
    /// instead of the client's own thresholds when present. Lenient: an
    /// odd value is nil, never a failed check.
    var presenceTone: ServerTone? = nil
    /// I10: the listing-strength figure's NAME ("Listing strength") and its
    /// band in words ("a few gaps", "not measured") — client_api.presence_band.
    /// The band is `presence_band_label`, not `presence_label`.
    var presenceLabel: String? = nil
    var presenceBandLabel: String? = nil
    /// I4: the AI-visibility chip read from the 90% RANGE, never the point
    /// (client_api.ai_visibility_band): band "often" | "sometimes" | "rarely"
    /// | "uncertain" | nil, its words and tone. Absent on an older server,
    /// which keeps the client's own point breakpoints.
    var aiScoreBand: String? = nil
    var aiScoreLabel: String? = nil
    var aiScoreTone: ServerTone? = nil
    let setupDone: Int?
    let setupTotal: Int?
    let claimKinds: [String: String]?
    let checklist: [AIVisibilityChecklistItem]?
    let gbpConnected: Bool?
    // Which AI system was actually asked. One vendor is sampled; the
    // interface used to call the result "AI search" and the roadmap named
    // three platforms that are never queried.
    let platform: String?
    let model: String?
    // Branded recall, kept apart from discovery.
    let brandedScore: Int?
    let brandedQueries: Int?
    // Which competitors surfaced in the same answers.
    let competitorAppearances: [CompetitorAppearance]?

    /// True only when every question came back AND we know the city. Any
    /// other state means the score is not a measurement of this restaurant.
    var scoreIsMeasured: Bool {
        (partial == false || partial == nil) && (locationKnown ?? true) && aiScore != nil
    }

    /// The AI-visibility chip's words, from the range (I4), sentence-cased —
    /// "Comes up sometimes", "Somewhere between 30% and 90% — too few
    /// questions to say more". Nil for an older server.
    var aiChipText: String? { aiScoreLabel.flatMap(Self.sentenceCase) }
    /// What the listing figure is called — the server's name for it.
    var presenceHeading: String { Self.sentenceCase(presenceLabel ?? "") ?? "Listing strength" }
    /// The listing band in words ("A few gaps"); nil for an older server.
    var presenceChipText: String? { presenceBandLabel.flatMap(Self.sentenceCase) }

    static func sentenceCase(_ raw: String) -> String? {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = t.first else { return nil }
        return first.uppercased() + t.dropFirst()
    }

    /// Why the score cannot be read as a measurement, or nil.
    var scoreCaveat: String? {
        if locationKnown == false {
            return "We don't have a city for this restaurant, so we can't tell your listing apart from another location with the same name. Add your Google Place ID in Account."
        }
        if aiScore == nil {
            return "\(platform ?? "AI search") didn't answer this time. This isn't a reading of your visibility — try again shortly."
        }
        if partial == true, let a = answeredQueries, let t = totalQueries {
            return "Only \(a) of \(t) questions came back, so this is an estimate rather than a measurement."
        }
        if citySource == "profile" {
            return "The city in these questions comes from your profile text, not from your Google listing."
        }
        return nil
    }
    // Posts PUBLISHED in the trailing 30 days (drafts no longer count) —
    // not a GBP field,
    // so it rides along outside gbp_score/checklist as its own count. Powers
    // the roadmap's "Post consistently on social" auto-done detection.
    let socialPosts30d: Int?
    // Real per-restaurant numbers behind the review-volume and response-rate
    // checklist items — previously computed server-side but never left
    // client_api.py, so the roadmap could only ever show a boolean done
    // flag instead of this restaurant's own actual counts. Now used to
    // build roadmap copy like "38 of 50 reviews" instead of identical
    // boilerplate for every restaurant.
    let reviewTotal: Int?
    let respRate: Double?
    let error: String?
    /// #35: when a served result was actually measured (the recorded run's
    /// stamp, ISO) and whether it was served rather than run now. Lenient;
    /// absent on a fresh run and on an older server.
    var measuredAt: LenientText? = nil
    var cached: LenientFlag? = nil

    /// "Measured 9/21/26" — on the phone's calendar day, never ISO.
    var measuredLine: String? {
        measuredAt?.value.map { "Measured " + CavnarDate.mdyLocal($0) }
    }

    /// Whole days since the check was run; nil without a readable stamp.
    func measuredAgeDays(now: Date = Date()) -> Int? {
        guard let raw = measuredAt?.value else { return nil }
        var when = CavnarDate.timestamp(raw)
        if when == nil {
            // A bare date: read it as that day on the phone's calendar.
            let p = raw.prefix(10).split(separator: "-").compactMap { Int($0) }
            if p.count == 3 {
                when = Calendar.current.date(from: DateComponents(year: p[0], month: p[1], day: p[2]))
            }
        }
        guard let when else { return nil }
        return Calendar.current.dateComponents([.day], from: Calendar.current.startOfDay(for: when),
                                               to: Calendar.current.startOfDay(for: now)).day
    }

    /// Seven days or more: the same rule and words as Intel's competitor
    /// snapshot (IntelSummary.stalenessNote) — read it as background.
    func backgroundNote(now: Date = Date()) -> String? {
        guard let d = measuredAgeDays(now: now), d >= 7 else { return nil }
        return "This check is \(d) days old. AI answers move; treat it as background, not as today's picture."
    }

    enum CodingKeys: String, CodingKey {
        case ok, error, queries, checklist, partial, city, roadmap, cached
        case measuredAt = "measured_at"
        case restaurantName = "restaurant_name"
        case appearedCount = "appeared_count"
        case totalQueries = "total_queries"
        case aiScore = "ai_score"
        case aiScoreLow = "ai_score_low"
        case aiScoreHigh = "ai_score_high"
        case answeredQueries = "answered_queries"
        case locationKnown = "location_known"
        case citySource = "city_source"
        case gbpScore = "gbp_score"
        case presenceScore = "presence_score"
        case presenceMeasured = "presence_measured"
        case presenceUnmeasured = "presence_unmeasured"
        case presenceTone = "presence_tone"
        case presenceLabel = "presence_label"
        case presenceBandLabel = "presence_band_label"
        case aiScoreBand = "ai_score_band"
        case aiScoreLabel = "ai_score_label"
        case aiScoreTone = "ai_score_tone"
        case setupDone = "setup_done"
        case setupTotal = "setup_total"
        case claimKinds = "claim_kinds"
        case gbpConnected = "gbp_connected"
        case platform, model
        case brandedScore = "branded_score"
        case brandedQueries = "branded_queries"
        case competitorAppearances = "competitor_appearances"
        case socialPosts30d = "social_posts_30d"
        case reviewTotal = "review_total"
        case respRate = "resp_rate"
    }
}

@Observable
@MainActor
final class AIVisibilityViewModel {
    var result: AIVisibilityResult?
    var isChecking = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// Deliberately NOT auto-loaded on screen appear — each check fires real,
    /// billable Perplexity queries (same 3-call/60s limit the web route
    /// shares), so this only runs when the owner explicitly asks for it.
    var history: [AIVisibilityRun] = []
    private struct HistoryResponse: Decodable { let ok: Bool; let runs: [AIVisibilityRun] }

    /// Every past check (ai_visibility_runs) — the Orbit's trend line.
    func loadHistory() async {
        if let response: HistoryResponse = try? await client.send("/mobile/api/intel/ai-visibility/history", hapticOnError: false) {
            history = response.runs
        }
    }

    func check() async {
        isChecking = true
        defer { isChecking = false }
        do {
            result = try await client.send("/mobile/api/intel/ai-visibility")
            await loadHistory()
        } catch {
            result = nil
        }
    }
}
