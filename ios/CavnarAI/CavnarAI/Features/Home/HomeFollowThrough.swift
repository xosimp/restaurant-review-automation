import SwiftUI
import Observation

/// Home's follow-through: what is still open, where the goals stand, what
/// the owner's own changes did, and the close-out handoff.
///
/// All of it existed on the web and none of it on the phone, which is the
/// device an owner actually has on the floor — so the accountability loop
/// (an issue with a name against it, a result that landed, tonight's
/// handoff) could only be closed at a desk. Same endpoints as the web:
/// /mobile/api/actions, /goals, /outcomes, /closeout.

// MARK: - Models

struct ActionItem: Decodable, Identifiable {
    struct Action: Decodable {
        /// The same endpoint on each client — the web and mobile APIs have
        /// different prefixes, so the server hands over both.
        struct Route: Decodable { let web: String?; let mobile: String? }
        /// What the route needs posted — a reprice's dish and price.
        struct Body: Codable { let dish: String?; let price: Double? }
        let label: String
        let route: Route?
        let method: String?
        let module: String?
        let body: Body?
    }
    let key: String
    let kind: String
    let title: String
    let detail: String?
    let severity: String
    let action: Action?
    var id: String { key }

    /// Home's own severity vocabulary, so these rows read exactly like the
    /// needs-attention ones above them.
    var tone: Color {
        switch severity {
        case "critical": return .cavnarRed
        case "important": return .cavnarAmber
        default: return .cavnarInk3
        }
    }
}

struct GoalRow: Decodable, Identifiable {
    let id: Int
    let summary: String?
    let label: String?
    let state: String?

    var tone: Color {
        switch state {
        case "met", "moving_right_way": return .cavnarGreen
        case "moving_wrong_way", "missed": return .cavnarRed
        default: return .cavnarInk3
        }
    }
}

/// "What your changes did" reads the full tracker row (RecOutcome) now —
/// the result line, its attribution sentence and whether the owner has
/// checked in on it — rather than the old summary-and-verdict pair.
extension RecOutcome {
    /// Green / red only for a result that counts (`standing`): one the
    /// owner disowned at check-in, one that faded at the re-check, or an
    /// informational row reads neutral whatever its verdict.
    var tone: Color {
        switch standing {
        case .good: return .cavnarGreen
        case .bad: return .cavnarRed
        case .neutral: return .cavnarInk3
        }
    }
}

struct CloseOutEntry: Decodable {
    let businessDate: String?
    let submittedBy: String?
    let wentWell: String?
    let wentWrong: String?
    let eightySixed: String?
    let callouts: String?
    // The six the nightly Daily Sales Report reads (closeout.DSR_FIELDS).
    // Optional: a backend from before them simply omits the keys.
    let equipment: String?
    let vipGuests: String?
    let maintenance: String?
    let shiftNotes: String?
    let generalNotes: String?
    let influence: String?

    enum CodingKeys: String, CodingKey {
        case businessDate = "business_date"
        case submittedBy = "submitted_by"
        case wentWell = "went_well"
        case wentWrong = "went_wrong"
        case eightySixed = "eighty_sixed"
        case callouts = "callouts"
        case equipment
        case vipGuests = "vip_guests"
        case maintenance
        case shiftNotes = "shift_notes"
        case generalNotes = "general_notes"
        case influence
    }
}

/// Everything a close-out can say, as the sheet edits it. All ten are sent
/// on every save: an empty one is a cleared one.
struct CloseOutDraft: Encodable {
    var wentWell = ""
    var wentWrong = ""
    var eightySixed = ""
    var callouts = ""
    var equipment = ""
    var vipGuests = ""
    var maintenance = ""
    var shiftNotes = ""
    var generalNotes = ""
    var influence = ""

    init() {}

    init(_ e: CloseOutEntry) {
        wentWell = e.wentWell ?? ""
        wentWrong = e.wentWrong ?? ""
        eightySixed = e.eightySixed ?? ""
        callouts = e.callouts ?? ""
        equipment = e.equipment ?? ""
        vipGuests = e.vipGuests ?? ""
        maintenance = e.maintenance ?? ""
        shiftNotes = e.shiftNotes ?? ""
        generalNotes = e.generalNotes ?? ""
        influence = e.influence ?? ""
    }

    var isEmpty: Bool {
        [wentWell, wentWrong, eightySixed, callouts, equipment, vipGuests,
         maintenance, shiftNotes, generalNotes, influence]
            .allSatisfy { $0.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    enum CodingKeys: String, CodingKey {
        case wentWell = "went_well"
        case wentWrong = "went_wrong"
        case eightySixed = "eighty_sixed"
        case callouts = "callouts"
        case equipment
        case vipGuests = "vip_guests"
        case maintenance
        case shiftNotes = "shift_notes"
        case generalNotes = "general_notes"
        case influence
    }
}

// MARK: - View model

@Observable
@MainActor
final class HomeFollowThroughViewModel {
    var actions: [ActionItem] = []
    var goals: [GoalRow] = []
    /// The finished trackers with a sentence to show ("What your changes did").
    var results: [RecOutcome] = []
    /// Every tracker row /outcomes returned — the check-ins read these.
    var outcomes: [RecOutcome] = []
    /// Home's "one thing" (GET /cross-module → fix_first), when there is one.
    var fixFirst: CrossModule.FixFirst?
    /// GET /recs/what-worked?days=180 — nil until loaded or on an older server.
    var whatWorked: WhatWorked?
    var closeOut: CloseOutEntry?
    var closeOutDate: String?
    var caveat: String?
    var value: ValueSummary?
    var links: [CrossModule.Link] = []
    var goodNews: [GoodNews.Item] = []
    var goodNewsCaveat: String?
    /// The one milestone to celebrate, if any. Cleared as soon as it is
    /// shown and marked seen, so it cannot reappear on the next refresh.
    var pendingMilestone: Milestones.Item?
    var lossFlags: [LossSignals.Flag] = []
    var lossWeek: [String] = []
    var lossNote: String?
    /// Last month against the month before (and last year), the same build
    /// the email on the 1st sends. Web-only until the retention re-audit.
    var month: HomeMonthlyReview?
    /// Recommendations the owner has started measuring in this session, so
    /// the button can read "Tracking" without a round trip.
    var tracked: Set<String> = []
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// Results that have landed and wait on the owner's check-in (#21) —
    /// at most two on Home; the rest wait in the recommendation record.
    var checkInsDue: [RecOutcome] { Array(outcomes.filter(RecCheckIn.isDue).prefix(2)) }

    private struct ActionsResponse: Decodable { let ok: Bool; let items: [ActionItem] }
    private struct GoalsResponse: Decodable { let ok: Bool; let goals: [GoalRow] }
    private struct CloseOutResponse: Decodable {
        let ok: Bool
        let closeout: CloseOutEntry?
        let businessDate: String?
        enum CodingKeys: String, CodingKey {
            case ok, closeout
            case businessDate = "business_date"
        }
    }
    private typealias OKResponse = APIClient.OKResponse

    /// GET /mobile/api/value. Four figures that are never added to each
    /// other — see value_delivered.py. Decoded leniently: an older backend
    /// that does not serve this route leaves `value` nil and the section
    /// simply does not appear.
    struct ValueSummary: Decodable {
        struct Delivered: Decodable {
            let monthly: Double?
            let annual: Double?
            let wins: Int?
            let evaluated: Int?
            let inFlight: Int?
            let unmeasurable: Int?
            let noClearChange: Int?
            let biggest: Biggest?
            let caveat: String?
            // Rec-ROI #1 / #14 / #34. `monthly` stays the improvements
            // alone; the changes that got worse sit BESIDE it and
            // `netMonthly` is the one less the other. All optional: an
            // older server sends none of them.
            let worsened: Worsened?
            let netMonthly: Double?
            let netNote: String?
            let validatedMonthly: Double?
            let validated: Int?
            let faded: Int?
            let cumulative: Cumulative?
            /// Measured wins on a number with no dollar rate (a rating
            /// rise): real, counted, never priced. Optional — an older
            /// server sends none.
            let unpricedWins: [UnpricedWin]?
            struct Biggest: Decodable { let title: String?; let monthly: Double?; let summary: String? }
            /// `count` is every result that got worse; `priced_count` the
            /// ones carrying dollars — the only ones `monthly` covers.
            struct Worsened: Decodable {
                let count: Int?
                let monthly: Double?
                let pricedCount: Int?
                enum CodingKeys: String, CodingKey {
                    case count, monthly
                    case pricedCount = "priced_count"
                }
                init(from decoder: Decoder) throws {
                    let c = try decoder.container(keyedBy: CodingKeys.self)
                    count = try? c.decodeIfPresent(Int.self, forKey: .count)
                    monthly = try? c.decodeIfPresent(Double.self, forKey: .monthly)
                    pricedCount = try? c.decodeIfPresent(Int.self, forKey: .pricedCount)
                }
            }
            struct UnpricedWin: Decodable {
                let line: String?
                let module: String?
                let title: String?
                init(from decoder: Decoder) throws {
                    let c = try decoder.container(keyedBy: CodingKeys.self)
                    line = try? c.decodeIfPresent(String.self, forKey: .line)
                    module = try? c.decodeIfPresent(String.self, forKey: .module)
                    title = try? c.decodeIfPresent(String.self, forKey: .title)
                }
                enum CodingKeys: String, CodingKey { case line, module, title }
            }
            enum CodingKeys: String, CodingKey {
                case monthly, annual, wins, evaluated, biggest, caveat
                case inFlight = "in_flight"
                case unmeasurable
                case noClearChange = "no_clear_change"
                case worsened, validated, faded, cumulative
                case netMonthly = "net_monthly"
                case netNote = "net_note"
                case validatedMonthly = "validated_monthly"
                case unpricedWins = "unpriced_wins"
            }
        }
        /// A SUM of measured days, net of what got worse — never a monthly
        /// figure times months. `total` is null when no day has been
        /// measured: nothing measured is not $0, and nothing is drawn.
        struct Cumulative: Decodable {
            let total: Double?
            let gained: Double?
            let lost: Double?
            let since: String?
            let until: String?
            let days: Int?
            let measuredDays: Int?
            let basis: String?
            enum CodingKeys: String, CodingKey {
                case total, gained, lost, since, until, days, basis
                case measuredDays = "measured_days"
            }
        }
        struct Avoided: Decodable {
            struct Item: Decodable {
                let key: String?
                let label: String?
                let dollars: Double?
                let hours: Double?
                let rate: String?
                let basis: String?
            }
            let items: [Item]?
            let dollars: Double?
            let hours: Double?
        }
        struct Opportunity: Decodable { let monthly: Double? }
        struct Surfaced: Decodable { let dollars: Double?; let alerts: Int?; let days: Int? }
        /// What was estimated at the in-person sales audit, against what has
        /// since been measured. This is the product keeping — or failing to
        /// keep — a promise it made at a table, and it shipped web-only:
        /// /api/value has carried it since the ROI audit and this struct
        /// simply did not decode it. Owner-level; the server omits it for a
        /// login without TEAM_INVITE, so `nil` is normal and not an error.
        struct Promise: Decodable {
            struct Annual: Decodable { let low: Double?; let high: Double? }
            struct Category: Decodable {
                let label: String?
                let metricLabel: String?
                let unit: String?
                let then: Double?
                let now: Double?
                let promisedLow: Double?
                let promisedHigh: Double?
                let note: String?
                enum CodingKeys: String, CodingKey {
                    case label, unit, then, now, note
                    case metricLabel = "metric_label"
                    case promisedLow = "promised_low"
                    case promisedHigh = "promised_high"
                }
            }
            let available: Bool?
            let auditDate: String?
            let promisedAnnual: Annual?
            let categories: [Category]?
            let caveat: String?
            enum CodingKeys: String, CodingKey {
                case available, categories, caveat
                case auditDate = "audit_date"
                case promisedAnnual = "promised_annual"
            }
        }
        /// Distinct work since sign-up — counts, not dollars. Needs no
        /// button, so it is the one "since you started" figure every
        /// account has on day one.
        struct Ledger: Decodable { let started: String?; let lines: [String]? }
        let ok: Bool
        let delivered: Delivered?
        let avoided: Avoided?
        let opportunity: Opportunity?
        let surfaced: Surfaced?
        let promise: Promise?
        let ledger: Ledger?

        /// Whether Home's worth card has anything to say. An unpriced win
        /// counts: a restaurant whose only measured win is a rating rise
        /// has a result, and hid the card entirely when only priced wins did.
        var showsWorthCard: Bool {
            guard let d = delivered else { return false }
            return (d.wins ?? 0) > 0 || !(d.unpricedWins ?? []).isEmpty
                || (d.inFlight ?? 0) > 0 || (d.worsened?.count ?? 0) > 0
                || d.cumulative?.total != nil
                || (avoided?.hours ?? 0) > 0 || (opportunity?.monthly ?? 0) > 0
        }
    }

    /// GET /mobile/api/cross-module — what two modules saw that neither
    /// could see alone. Every field the web card shows, because the honesty
    /// furniture (what would confirm it, what else explains it) is not
    /// optional decoration: the link is a question, and rendering only the
    /// headline turns it into a finding.
    struct CrossModule: Decodable {
        struct Link: Decodable, Identifiable {
            let kind: String?
            let headline: String
            let modules: [String]?
            let evidence: [String]?
            let confirmBy: String?
            let alternative: String?
            let notACause: String?
            let ask: String?
            /// `link:<kind>:<subject>` — what Done / Not for us answer
            /// (rec-ROI #26). Absent on an older server.
            let recKey: String?
            let answerable: Bool?
            var id: String { headline }
            enum CodingKeys: String, CodingKey {
                case kind, headline, modules, evidence, alternative, ask, answerable
                case confirmBy = "confirm_by"
                case notACause = "not_a_cause"
                case recKey = "rec_key"
            }
        }
        /// "If you only do one thing" — business_intelligence.pick_one_thing:
        /// always an action (the top driver's fix, a diagnosis's action, or
        /// the replies that are owed), presented on `home` with its key.
        struct FixFirst: Decodable, Equatable {
            let key: String?
            let what: String?
            let why: String?
            let modules: [String]?
            let evidence: [String]?
            let dollarsMonthly: Double?
            let alternative: String?
            let confirmBy: String?
            let linkHeadline: String?
            let recKey: String?
            let answerable: Bool?
            enum CodingKeys: String, CodingKey {
                case key, what, why, modules, evidence, alternative, answerable
                case dollarsMonthly = "dollars_monthly"
                case confirmBy = "confirm_by"
                case linkHeadline = "link_headline"
                case recKey = "rec_key"
            }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                key = try? c.decodeIfPresent(String.self, forKey: .key)
                what = try? c.decodeIfPresent(String.self, forKey: .what)
                why = try? c.decodeIfPresent(String.self, forKey: .why)
                modules = try? c.decodeIfPresent([String].self, forKey: .modules)
                evidence = try? c.decodeIfPresent([String].self, forKey: .evidence)
                dollarsMonthly = try? c.decodeIfPresent(Double.self, forKey: .dollarsMonthly)
                alternative = try? c.decodeIfPresent(String.self, forKey: .alternative)
                confirmBy = try? c.decodeIfPresent(String.self, forKey: .confirmBy)
                linkHeadline = try? c.decodeIfPresent(String.self, forKey: .linkHeadline)
                recKey = try? c.decodeIfPresent(String.self, forKey: .recKey)
                answerable = try? c.decodeIfPresent(Bool.self, forKey: .answerable)
            }
            /// The key its answer row posts — rec_key, else the pick's own key.
            var answerKey: String? { recKey ?? key }
        }
        let ok: Bool
        let links: [Link]?
        let fixFirst: FixFirst?
        enum CodingKeys: String, CodingKey {
            case ok, links
            case fixFirst = "fix_first"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
            links = try? c.decodeIfPresent([Link].self, forKey: .links)
            fixFirst = try? c.decodeIfPresent(FixFirst.self, forKey: .fixFirst)
        }
    }

    /// GET /mobile/api/good-news — records, streaks and complaints that
    /// stopped.
    struct GoodNews: Decodable {
        struct Item: Decodable, Identifiable {
            let kind: String
            let key: String
            let headline: String
            let summary: String?
            let module: String?
            let ask: String?
            var id: String { key }
        }
        let ok: Bool
        let items: [Item]?
        let caveat: String?
    }

    /// GET /mobile/api/loss-signals — comps, voids and refunds against
    /// their own eight-week baseline.
    ///
    /// Owner-level, or a manager the owner explicitly granted LOSS_VIEW:
    /// a signal can name the approving manager, so the server returns 403
    /// otherwise and `nil` here is normal rather than an error.
    ///
    /// `alternative` is not optional decoration. loss_detection attributes
    /// to the APPROVER because a comp is a manager's decision, and ships an
    /// innocent explanation for every signal — rendering a headline without
    /// it turns a question into an accusation about a named person.
    struct LossSignals: Decodable {
        struct Flag: Decodable, Identifiable {
            let type: String?
            let headline: String
            let alternative: String?
            /// `loss:<week start>:<kind>:spike` (or `…:<approver>`) — the key
            /// the concentration issue is filed under; presented on `home`.
            let key: String?
            let recKey: String?
            let answerable: Bool?
            var id: String { headline }
            enum CodingKeys: String, CodingKey {
                case type, headline, alternative, key, answerable
                case recKey = "rec_key"
            }
        }
        let ok: Bool
        let available: Bool?
        let week: [String]?
        let flagged: [Flag]?
        let note: String?
    }

    /// GET /mobile/api/milestones — moments worth marking. `unseen` drives
    /// the one-time celebration; the row is the once-ever guarantee, so
    /// marking it seen here holds on the web too.
    struct Milestones: Decodable {
        struct Item: Decodable, Identifiable {
            let kind: String
            let key: String
            let title: String
            let body: String?
            var id: String { key }
        }
        let ok: Bool
        let items: [Item]?
        let unseen: [Item]?
    }

    /// Every block is optional: a login without an endpoint's permission, or
    /// a module it can't see, simply shows fewer rows rather than an error.
    func load() async {
        isLoading = actions.isEmpty && goals.isEmpty && results.isEmpty
        defer { isLoading = false }
        async let a: ActionsResponse? = try? client.send("/mobile/api/actions", hapticOnError: false)
        async let g: GoalsResponse? = try? client.send("/mobile/api/goals", hapticOnError: false)
        async let o: RecOutcomesResponse? = try? client.send("/mobile/api/outcomes", hapticOnError: false)
        // 180 days: the server's default window and the monthly email's,
        // so Home says the same sentences the owner was emailed.
        async let ww: WhatWorked? = try? client.send("/mobile/api/recs/what-worked", query: ["days": "180"],
                                                     hapticOnError: false)
        async let c: CloseOutResponse? = try? client.send("/mobile/api/closeout", hapticOnError: false)
        async let v: ValueSummary? = try? client.send("/mobile/api/value", hapticOnError: false)
        async let x: CrossModule? = try? client.send("/mobile/api/cross-module", hapticOnError: false)
        async let n: GoodNews? = try? client.send("/mobile/api/good-news", hapticOnError: false)
        async let ms: Milestones? = try? client.send("/mobile/api/milestones", hapticOnError: false)
        async let ls: LossSignals? = try? client.send("/mobile/api/loss-signals", hapticOnError: false)
        async let mr: HomeMonthlyReview? = try? client.send("/mobile/api/monthly-review", hapticOnError: false)
        actions = (await a)?.items ?? []
        goals = (await g)?.goals ?? []
        let fetchedOutcomes = await o
        outcomes = fetchedOutcomes?.outcomes ?? []
        results = outcomes.filter { $0.summary?.isEmpty == false }
        caveat = fetchedOutcomes?.caveat
        whatWorked = await ww
        let close = await c
        closeOut = close?.closeout
        closeOutDate = close?.businessDate
        value = await v
        let cross = await x
        fixFirst = cross?.fixFirst.flatMap { ($0.what ?? "").isEmpty ? nil : $0 }
        // The one thing owns a link it leads with — the same finding is
        // never on the page twice (the web's renderConnections rule).
        let owned = fixFirst.flatMap { $0.linkHeadline ?? $0.what }
        links = (cross?.links ?? []).filter { $0.headline != owned }
        let news = await n
        goodNews = news?.items ?? []
        goodNewsCaveat = news?.caveat
        // Only ever offer one, and only one that has not been shown on any
        // device — the server's UNIQUE row is what makes that true.
        pendingMilestone = (await ms)?.unseen?.first
        let loss = await ls
        lossFlags = (loss?.available == true) ? (loss?.flagged ?? []) : []
        lossWeek = loss?.week ?? []
        lossNote = loss?.note
        month = await mr
    }

    private struct TrackBody: Encodable {
        let source: String
        let sourceKey: String
        let title: String
        let metric: String
        enum CodingKeys: String, CodingKey {
            case source, title, metric
            case sourceKey = "source_key"
        }
    }
    private struct TrackResponse: Decodable {
        struct Outcome: Decodable {
            let evaluateOn: String?
            enum CodingKeys: String, CodingKey { case evaluateOn = "evaluate_on" }
        }
        let ok: Bool
        let outcome: Outcome?
        let warning: String?
        let error: String?
        /// Exactly one of these (API_REFERENCE → Tracker-start replies).
        let tracker: RecTracker?
        let trackerRefused: RecTrackerRefused?
        enum CodingKeys: String, CodingKey {
            case ok, outcome, warning, error, tracker
            case trackerRefused = "tracker_refused"
        }
    }

    /// Start measuring a recommendation. The baseline is taken server-side
    /// at the moment this posts — nothing about the card's own numbers is
    /// sent, so the measurement cannot inherit a stale figure off the
    /// screen. Same contract as the web's hbTrack.
    ///
    /// The line it returns says what is measured until when ("Measuring
    /// labor % until 10/21/26"), or — when another change is already being
    /// measured on the same number — the server's reason nothing started.
    /// Either way the answer was recorded as taken.
    @discardableResult
    func track(_ rec: HomeRecommendation) async -> String? {
        guard let metric = rec.metric else { return nil }
        do {
            let r: TrackResponse = try await client.send(
                "/mobile/api/outcomes", method: .post,
                body: TrackBody(source: "recommendation", sourceKey: rec.key,
                                title: rec.title, metric: metric),
                retryTransient: false)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn't start tracking that."
                return nil
            }
            tracked.insert(rec.key)
            await Haptic.success()
            await load()
            if let refused = RecTrackerNote.line(tracker: nil, refused: r.trackerRefused) { return refused }
            let line = RecTrackerNote.line(tracker: r.tracker, refused: nil)
            if let warning = r.warning { return [line, warning].compactMap { $0 }.joined(separator: ". ") }
            if let line { return line }
            if let on = r.outcome?.evaluateOn { return "Measuring from today — result on \(CavnarDate.mdy(on))" }
            return "Measuring from today"
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return nil
        } catch {
            errorMessage = "Couldn't start tracking that."
            return nil
        }
    }

    private struct DismissBody: Encodable {
        let key: String
        let kind: String
        let title: String?
        let metric: String?
        var reason: String? = nil
        /// The one-tap why (RecReason) — rec_ledger.REASON_CODES.
        var reasonCode: String? = nil

        enum CodingKeys: String, CodingKey {
            case key, kind, title, metric, reason
            case reasonCode = "reason_code"
        }
    }

    /// "Done", "Not for us" or "Hide" (kind "recommendation") on a
    /// recommendation, with the owner's reason when they gave one. Done with
    /// a metric records an observed outcome — the same thing Track this does.
    /// Every answer reaches rec_ledger server-side, so it holds on the
    /// brief, the emails and the queue too. Returns a line for the
    /// confirmation.
    func answer(_ rec: HomeRecommendation, kind: String, reason: String? = nil,
                reasonCode: String? = nil) async -> String? {
        struct Resp: Decodable {
            struct Outcome: Decodable {
                let evaluateOn: String?
                enum CodingKeys: String, CodingKey { case evaluateOn = "evaluate_on" }
            }
            let ok: Bool; let outcome: Outcome?; let error: String?
            let tracker: RecTracker?
            let trackerRefused: RecTrackerRefused?
            enum CodingKeys: String, CodingKey {
                case ok, outcome, error, tracker
                case trackerRefused = "tracker_refused"
            }
        }
        do {
            let r: Resp = try await client.send(
                "/mobile/api/home/dismiss", method: .post,
                body: DismissBody(key: rec.key, kind: kind,
                                  title: (kind == "done" || reason != nil || reasonCode != nil) ? rec.title : nil,
                                  metric: kind == "done" ? rec.metric : nil,
                                  reason: reason, reasonCode: reasonCode),
                retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that."; return nil }
            await Haptic.success()
            if kind == "done" {
                // What Done started measuring, or why nothing did (another
                // change already on the same number).
                if let line = RecTrackerNote.line(tracker: r.tracker, refused: r.trackerRefused) {
                    return "Marked done \u{2014} " + line.prefix(1).lowercased() + line.dropFirst()
                }
                if let on = r.outcome?.evaluateOn {
                    return "Marked done — measuring from today, result on \(CavnarDate.mdy(on))"
                }
            }
            switch kind {
            case "done": return "Marked done"
            case "recommendation": return "Hidden for two weeks"
            default: return "Noted — it won\u{2019}t come back"
            }
        } catch { errorMessage = "Couldn\u{2019}t save that."; return nil }
    }

    /// Not today (kind "snooze") or hide (kind "recommendation") on a
    /// Needs-attention item — the same answer, through the same route, as a
    /// recommendation. Never offered for a critical item. A second hide
    /// asks why, and that answer arrives as not_for_us with its reason code.
    @discardableResult
    func answerAttention(_ item: NeedsAttentionItem, kind: String, reason: String? = nil,
                         reasonCode: String? = nil) async -> Bool {
        let r: OKResponse? = try? await client.send(
            "/mobile/api/home/dismiss", method: .post,
            body: DismissBody(key: item.recKey ?? item.type, kind: kind,
                              title: (reason != nil || reasonCode != nil) ? item.title : nil, metric: nil,
                              reason: reason, reasonCode: reasonCode),
            retryTransient: false)
        if r?.ok == true { await Haptic.success() }
        return r?.ok == true
    }

    private struct AssignBody: Encodable {
        let key: String
        let title: String
        let detail: String?
        let contactId: Int
        enum CodingKeys: String, CodingKey {
            case key, title, detail
            case contactId = "contact_id"
        }
    }

    /// Hand a card to a person: an issue keyed to the recommendation,
    /// texted to that routed contact (POST /mobile/api/home/assign).
    func assign(_ rec: HomeRecommendation, to person: HomeAssignee) async -> String? {
        struct Resp: Decodable {
            struct Issue: Decodable {
                let assigneeName: String?
                enum CodingKeys: String, CodingKey { case assigneeName = "assignee_name" }
            }
            let ok: Bool; let issue: Issue?; let error: String?
        }
        do {
            let r: Resp = try await client.send(
                "/mobile/api/home/assign", method: .post,
                body: AssignBody(key: rec.key, title: rec.title, detail: rec.why, contactId: person.id),
                retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t hand that over."; return nil }
            await Haptic.success()
            return "Handed to \(r.issue?.assigneeName ?? person.name) — it\u{2019}s on the issues list"
        } catch { errorMessage = "Couldn\u{2019}t hand that over."; return nil }
    }

    private struct RepriceBody: Encodable { let dish: String; let price: Double }
    private struct RecEventBody: Encodable { let key: String; let event: String; let surface: String }

    /// One-tap reprice at the suggested price (the food-cost reprice route),
    /// then the recommendation is recorded as completed.
    func reprice(_ rec: HomeRecommendation) async -> String? {
        guard let a = rec.action, a.kind == "reprice", let dish = a.dish, let price = a.price else { return nil }
        let r: RepriceApplyResult? = try? await client.send(
            "/mobile/api/food-cost/reprice/apply", method: .post,
            body: RepriceBody(dish: dish, price: price), retryTransient: false)
        guard let r, r.ok else { errorMessage = "Couldn\u{2019}t reprice that."; return nil }
        _ = try? await client.send("/mobile/api/recs/event", method: .post,
                                   body: RecEventBody(key: rec.key, event: "completed", surface: "home"),
                                   hapticOnError: false) as OKResponse
        await Haptic.success()
        // What the new price is measured on until when, or why nothing is.
        if let line = RecTrackerNote.line(tracker: r.tracker, refused: r.trackerRefused) {
            return "\(dish) repriced \u{2014} " + line.prefix(1).lowercased() + line.dropFirst()
        }
        return "\(dish) repriced"
    }

    private struct RestoreBody: Encodable {
        let restoreKind: String
        enum CodingKeys: String, CodingKey { case restoreKind = "restore_kind" }
    }

    /// Bring back a kind that went quieter after four unanswered showings.
    @discardableResult
    func restoreKind(_ kind: String) async -> Bool {
        let r: OKResponse? = try? await client.send("/mobile/api/home/dismiss", method: .post,
                                                    body: RestoreBody(restoreKind: kind), retryTransient: false)
        return r?.ok == true
    }

    private struct SeenBody: Encodable { let key: String }

    func markMilestoneSeen(_ m: Milestones.Item) async {
        pendingMilestone = nil
        _ = try? await client.send("/mobile/api/milestones/seen", method: .post,
                                   body: SeenBody(key: m.key),
                                   hapticOnError: false) as OKResponse
    }

    private struct SnoozeBody: Encodable { let key: String }

    func snooze(_ item: ActionItem) async {
        actions.removeAll { $0.key == item.key }       // it's back tomorrow
        _ = try? await client.send("/mobile/api/actions/snooze", method: .post,
                                   body: SnoozeBody(key: item.key),
                                   hapticOnError: false) as OKResponse
        await load()
    }

    func run(_ item: ActionItem) async {
        guard let route = item.action?.route?.mobile else { return }
        let done: OKResponse?
        if let body = item.action?.body {
            done = try? await client.send(route, method: .post, body: body, retryTransient: false)
        } else {
            done = try? await client.send(route, method: .post, retryTransient: false)
        }
        if done?.ok == true { await Haptic.success() }
        await load()
    }

    @discardableResult
    func saveCloseOut(_ draft: CloseOutDraft) async -> Bool {
        errorMessage = nil
        do {
            let r: OKResponse = try await client.send(
                "/mobile/api/closeout", method: .post,
                body: draft,
                retryTransient: false)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn't save that."
                return false
            }
            await load()
            return true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't save that."
            return false
        }
    }
}

// MARK: - Section

struct HomeFollowThrough: View {
    let viewModel: HomeFollowThroughViewModel
    /// False when HomeView renders the close-out in the day's slot (after 8pm).
    var showsCloseOut: Bool = true
    /// When /mobile/api/home last answered (HomeViewModel.lastLoadedAt).
    /// The queue drops what Home already showed today, which it learns from
    /// Home's own fetch — so it is read only after that fetch, never
    /// against a cached summary painted first (the queue then repeated a
    /// needs-attention item on the same screen), and again after each one.
    var homeLoadedAt: Date? = nil
    var onOpenModule: (String) -> Void

    /// The recommendation record (RecommendationHistoryView), opened from
    /// "What your changes did" and from the worth card.
    @State private var showingRecord = false

    private var hasAnything: Bool {
        !viewModel.actions.isEmpty || !viewModel.goals.isEmpty || !viewModel.results.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if !viewModel.actions.isEmpty {
                HomeSectionHeader(kicker: "Follow-through", title: "Still open",
                                  trailing: "\(viewModel.actions.count)")
                VStack(spacing: 0) {
                    ForEach(Array(viewModel.actions.enumerated()), id: \.element.id) { index, item in
                        actionRow(item, showsDivider: index < viewModel.actions.count - 1)
                    }
                }
                .cavnarCard()
            }

            if !viewModel.goals.isEmpty {
                HomeSectionHeader(kicker: "Where you're heading", title: "Goals")
                VStack(spacing: 0) {
                    ForEach(Array(viewModel.goals.enumerated()), id: \.element.id) { index, goal in
                        lineRow(goal.summary ?? goal.label ?? "", tone: goal.tone,
                                showsDivider: index < viewModel.goals.count - 1)
                    }
                }
                .cavnarCard()
            }

            if !viewModel.results.isEmpty || !viewModel.checkInsDue.isEmpty {
                HomeSectionHeader(kicker: "Measured", title: "What your changes did")
                if !viewModel.results.isEmpty {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(viewModel.results.enumerated()), id: \.element.id) { index, r in
                            resultRow(r, showsDivider: index < viewModel.results.count - 1)
                        }
                        if let caveat = viewModel.caveat {
                            CavnarCaveat(title: "Before and after, not proof", detail: caveat)
                                .padding(.top, 10)
                        }
                        recordLink.padding(.top, 10)
                    }
                    .cavnarCard()
                }
                // A result that landed asks whether the owner made the
                // change — the answer changes how the result reads.
                ForEach(viewModel.checkInsDue) { outcome in
                    RecCheckInCard(outcome: outcome, surface: "home") { await viewModel.load() }
                }
            }

            goodNewsCard

            if let worked = viewModel.whatWorked {
                WhatWorkedCard(whatWorked: worked)
            }

            valueCard

            if let month = viewModel.month {
                HomeMonthlyReviewCard(month: month)
            }

            connectionsCard

            lossCard

            // Before 8pm the handoff sits here, at the end; after 8pm
            // HomeView puts it in the day's slot instead (HomeCloseOutCard).
            if showsCloseOut {
                HomeCloseOutCard(viewModel: viewModel)
            }
        }
        .task(id: homeLoadedAt) {
            guard homeLoadedAt != nil else { return }
            await viewModel.load()
        }
        .sheet(isPresented: $showingRecord, onDismiss: { Task { await viewModel.load() } }) {
            RecommendationHistoryView()
        }
    }

    /// "What you followed →" — the owner's recommendation record.
    private var recordLink: some View {
        Button {
            Haptic.light()
            showingRecord = true
        } label: {
            Text("What you followed \u{2192}")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarEmber2)
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens every recommendation, what you did about it, and what it did")
    }

    /// One result: its line, and under it what the result can honestly be
    /// credited with (the attribution sentence) — never a claim of cause.
    private func resultRow(_ r: RecOutcome, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Circle().fill(r.tone).frame(width: 8, height: 8).padding(.top, 6)
                VStack(alignment: .leading, spacing: 3) {
                    HomeMixedText.make(r.summary ?? r.resultLine ?? "", size: 14.5, weight: 500, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    if let label = r.attributionLabel, !label.isEmpty {
                        HomeMixedText.make(label, size: 12.5, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 11)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }

    /// What got better. Everything else on this screen looks for trouble —
    /// a restaurant that quietly improved used to hear exactly the same
    /// from Cavnar AI as one that did not.
    @ViewBuilder
    private var goodNewsCard: some View {
        if !viewModel.goodNews.isEmpty {
            HomeSectionHeader(kicker: "Measured", title: "What got better")
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(viewModel.goodNews.prefix(4).enumerated()), id: \.element.id) { index, item in
                    VStack(spacing: 0) {
                        HStack(alignment: .top, spacing: 12) {
                            Circle().fill(Color.cavnarGreen).frame(width: 8, height: 8)
                                .padding(.top, 6)
                            VStack(alignment: .leading, spacing: 3) {
                                HomeMixedText.make(item.headline, size: 14.5, weight: 700,
                                                   color: .cavnarInk)
                                if let summary = item.summary {
                                    HomeMixedText.make(summary, size: 12.5, weight: 500,
                                                       color: .cavnarInk3)
                                }
                                HomeAskLink(question: item.ask ?? "What's behind this: \(item.headline)",
                                            label: "Ask")
                                    .padding(.top, 2)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(.vertical, 11)
                        if index < min(viewModel.goodNews.count, 4) - 1 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        }
                    }
                }
                if let caveat = viewModel.goodNewsCaveat {
                    CavnarCaveat(title: "News, not a receipt", detail: caveat)
                        .padding(.top, 10)
                }
            }
            .cavnarCard()
        }
    }

    /// Comps, voids and refunds running above their own baseline, and any
    /// one manager accounting for most of a kind's dollars.
    ///
    /// The innocent explanation is rendered with the same weight as the
    /// signal, never behind a disclosure. This card names a person's POS id
    /// next to a percentage, and an owner reading that without "they may
    /// simply have worked the busiest shifts" will reach a conclusion the
    /// data does not support.
    @ViewBuilder
    private var lossCard: some View {
        if !viewModel.lossFlags.isEmpty {
            HomeSectionHeader(kicker: "Comps, voids and refunds", title: "Worth reviewing",
                              trailing: viewModel.lossWeek.count == 2 ? viewModel.lossWeek[1] : nil)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(viewModel.lossFlags.enumerated()), id: \.element.id) { index, flag in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .top, spacing: 12) {
                            Circle().fill(Color.cavnarAmber).frame(width: 8, height: 8)
                                .padding(.top, 6)
                            HomeMixedText.make(flag.headline, size: 14.5, weight: 600,
                                               color: .cavnarInk)
                            Spacer(minLength: 0)
                        }
                        if let alt = flag.alternative {
                            HomeMixedText.make("Could also be: " + alt, size: 12.5, weight: 500,
                                               color: .cavnarInk3)
                                .padding(.leading, 20)
                        }
                        // Done / Not for us — the key the concentration
                        // issue is filed under, presented on Home.
                        if flag.answerable == true, let key = flag.recKey ?? flag.key {
                            RecAnswerRow(key: key, surface: "home", module: "ops")
                                .padding(.leading, 20)
                        }
                        if index < viewModel.lossFlags.count - 1 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        }
                    }
                    .padding(.vertical, 11)
                }
                if let note = viewModel.lossNote {
                    CavnarCaveat(title: "A pattern, not a finding", detail: note)
                        .padding(.top, 6)
                }
            }
            .cavnarCard()
        }
    }

    /// Two modules agreeing is the strongest evidence this platform can
    /// produce, and the one finding no single-module tool can reach. It is
    /// a QUESTION, not a finding — so the evidence, what would confirm it
    /// and what else would explain it travel with the headline rather than
    /// hiding behind a chevron.
    @ViewBuilder
    private var connectionsCard: some View {
        if !viewModel.links.isEmpty {
            HomeSectionHeader(kicker: "Across your modules", title: "What connects",
                              trailing: "\(viewModel.links.count)")
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(viewModel.links.enumerated()), id: \.element.id) { index, link in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(alignment: .top, spacing: 12) {
                            Circle().fill(Color.cavnarAmber).frame(width: 8, height: 8)
                                .padding(.top, 6)
                            VStack(alignment: .leading, spacing: 4) {
                                HomeMixedText.make(link.headline, size: 14.5, weight: 700,
                                                   color: .cavnarInk)
                                if let modules = link.modules, !modules.isEmpty {
                                    HStack(spacing: 8) {
                                        EmberThread(axis: .horizontal, length: 34)
                                        Text(modules.map { $0.capitalized }.joined(separator: " + "))
                                            .font(.cavnarBody(11, weight: 700))
                                            .tracking(1.0)
                                            .foregroundStyle(Color.cavnarInk3)
                                    }
                                }
                            }
                            Spacer(minLength: 0)
                        }
                        if let evidence = link.evidence, !evidence.isEmpty {
                            VStack(alignment: .leading, spacing: 4) {
                                ForEach(evidence, id: \.self) { line in
                                    HomeMixedText.make("· " + line, size: 12.5, weight: 500,
                                                       color: .cavnarInk3)
                                }
                            }
                            .padding(.leading, 20)
                        }
                        if let confirm = link.confirmBy {
                            HomeMixedText.make("To confirm: " + confirm, size: 12.5, weight: 600,
                                               color: .cavnarInk2)
                                .padding(.leading, 20)
                        }
                        if let alt = link.notACause ?? link.alternative {
                            CavnarCaveat(title: "A question, not a finding", detail: alt)
                        }
                        HomeAskLink(question: link.ask ?? "Tell me more about this: \(link.headline)")
                            .padding(.leading, 20)
                        if link.answerable == true, let key = link.recKey {
                            RecAnswerRow(key: key, surface: "home", module: "home")
                                .padding(.leading, 20)
                        }
                        if index < viewModel.links.count - 1 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        }
                    }
                    .padding(.vertical, 11)
                }
            }
            .cavnarCard(.ai)
        }
    }

    /// What Cavnar AI has been worth. Four figures, never added together —
    /// a measurement, an estimate at stated rates, an alert total and a gap
    /// against target are not addends (see value_delivered.py).
    @ViewBuilder
    private var valueCard: some View {
        if let v = viewModel.value, let d = v.delivered, v.showsWorthCard {
            HomeSectionHeader(kicker: "Worth", title: "What Cavnar AI has been worth")
            VStack(alignment: .leading, spacing: 0) {
                let unpriced = RecValueFormat.unpricedWinLines(d)
                if (d.wins ?? 0) > 0 {
                    lineRow(Self.deliveredLine(d), tone: .cavnarGreen, showsDivider: true)
                    if let big = d.biggest?.summary {
                        lineRow("Biggest so far: " + big, tone: .cavnarInk3, showsDivider: true)
                    }
                } else if let lead = RecValueFormat.nothingPricedLine(d, unpricedWins: unpriced.count) {
                    lineRow(lead, tone: .cavnarInk3, showsDivider: true)
                }
                // A win on a number with no dollar rate (a rating rise) is
                // still a measured win — listed, never priced.
                ForEach(Array(unpriced.enumerated()), id: \.offset) { _, line in
                    lineRow(line, tone: .cavnarGreen, showsDivider: true)
                }
                // What got worse sits BESIDE the improvements, never folded
                // into them (rec-ROI #1); the server's own sentence says so
                // when the net is below zero.
                if let net = RecValueFormat.netLine(d) {
                    lineRow(net, tone: (d.netMonthly ?? 0) < 0 ? .cavnarRed : .cavnarInk3, showsDivider: true)
                }
                if let note = RecValueFormat.netNote(d) {
                    lineRow(note, tone: .cavnarRed, showsDivider: true)
                }
                if let counts = RecValueFormat.countsLine(d) {
                    lineRow(counts, tone: .cavnarInk3, showsDivider: true)
                }
                // A sum of measured days — drawn only when a day has been
                // measured (total null is "nothing yet", never $0).
                if let cumulative = RecValueFormat.cumulativeLine(d.cumulative) {
                    lineRow(cumulative, tone: (d.cumulative?.total ?? 0) < 0 ? .cavnarRed : .cavnarGreen,
                            showsDivider: true)
                }
                if let denom = Self.denominatorLine(d) {
                    lineRow(denom, tone: .cavnarInk3, showsDivider: true)
                }
                if let av = v.avoided, let line = Self.avoidedLine(av) {
                    lineRow(line, tone: .cavnarInk3, showsDivider: true)
                }
                if let sf = v.surfaced, let dollars = sf.dollars, dollars > 0 {
                    lineRow("\(Self.money(dollars)) of problems put in front of you across "
                            + "\(sf.alerts ?? 0) alerts in the last \(sf.days ?? 30) days",
                            tone: .cavnarInk3, showsDivider: true)
                }
                if let op = v.opportunity?.monthly, op > 0 {
                    lineRow("\(Self.money(op))/month still on the table — available, not captured",
                            tone: .cavnarEmber, showsDivider: false)
                }
                if let caveat = d.caveat {
                    CavnarCaveat(title: "Before and after, not proof", detail: caveat)
                        .padding(.top, 10)
                }
                promiseBlock(v.promise)
                if let lines = v.ledger?.lines, !lines.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("SINCE YOU STARTED")
                            .font(.cavnarBody(11, weight: 700))
                            .tracking(1.4)
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.top, 10)
                        ForEach(lines.prefix(6), id: \.self) { line in
                            HomeMixedText.make(line, size: 13.5, weight: 500, color: .cavnarInk2)
                        }
                    }
                }
                recordLink.padding(.top, 12)
            }
            .cavnarCard()
        }
    }

    /// "At your audit on March 3 we estimated $40k–$70k a year was
    /// available. Here is what happened." The product keeping — or failing
    /// to keep — a promise it made in person, which is the strongest thing
    /// it can say and shipped web-only until now.
    @ViewBuilder
    private func promiseBlock(_ promise: HomeFollowThroughViewModel.ValueSummary.Promise?) -> some View {
        if let p = promise, p.available == true,
           let annual = p.promisedAnnual, let low = annual.low, low > 0 {
            VStack(alignment: .leading, spacing: 8) {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    .padding(.vertical, 6)
                Text("THE PROMISE")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.4)
                    .foregroundStyle(Color.cavnarEmber2)
                HomeMixedText.make(
                    "At your audit on \(p.auditDate ?? "sign-up") we estimated "
                    + "\(Self.money(low))–\(Self.money(annual.high ?? low)) a year was available.",
                    size: 14, weight: 600, color: .cavnarInk)
                ForEach(Array((p.categories ?? []).enumerated()), id: \.offset) { _, c in
                    if let note = c.note {
                        HomeMixedText.make("\(c.label ?? c.metricLabel ?? ""): \(note)",
                                           size: 12.5, weight: 500, color: .cavnarInk3)
                    } else if let then = c.then, let now = c.now {
                        HomeMixedText.make(
                            "\(c.metricLabel ?? c.label ?? ""): \(Self.trim(then))\(c.unit ?? "") "
                            + "at the audit, \(Self.trim(now))\(c.unit ?? "") now",
                            size: 12.5, weight: 500, color: .cavnarInk3)
                    }
                }
                if let caveat = p.caveat {
                    CavnarCaveat(title: "What was estimated, against what was measured",
                                 detail: caveat)
                }
            }
            .padding(.top, 4)
        }
    }

    /// 27.0 reads as a measurement; 27 reads as a number someone typed.
    /// Both are wrong for the other one, so trim only the trailing zero.
    private static func trim(_ v: Double) -> String {
        v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v)
    }

    private static func money(_ v: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        return "$" + (f.string(from: NSNumber(value: v)) ?? String(Int(v)))
    }

    private static func deliveredLine(_ d: HomeFollowThroughViewModel.ValueSummary.Delivered) -> String {
        let wins = d.wins ?? 0
        var s = "\(money(d.monthly ?? 0))/month measured across \(wins) change"
        if wins != 1 { s += "s" }
        if let annual = d.annual, annual > 0 {
            s += " — about \(money(annual)) a year if it holds"
        }
        return s + "."
    }

    /// The denominator always travels with the total: "2 results" reads
    /// differently when 8 others came back unreadable.
    private static func denominatorLine(_ d: HomeFollowThroughViewModel.ValueSummary.Delivered) -> String? {
        let evaluated = d.evaluated ?? 0
        let unknown = d.unmeasurable ?? 0
        let flat = d.noClearChange ?? 0
        guard evaluated > 0, unknown > 0 || flat > 0 else { return nil }
        var bits: [String] = []
        if flat > 0 { bits.append("\(flat) showed no clear change") }
        if unknown > 0 { bits.append("\(unknown) couldn't be measured") }
        return "Of \(evaluated) finished: " + bits.joined(separator: ", ") + "."
    }

    private static func avoidedLine(_ av: HomeFollowThroughViewModel.ValueSummary.Avoided) -> String? {
        var bits: [String] = []
        if let h = av.hours, h > 0 { bits.append("\(Int(h.rounded())) hours of work done for you") }
        if let d = av.dollars, d > 0 { bits.append("\(money(d)) you'd otherwise have paid for") }
        guard !bits.isEmpty else { return nil }
        return bits.joined(separator: " · ") + " (estimates at stated rates)"
    }

    private func actionRow(_ item: ActionItem, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Circle().fill(item.tone).frame(width: 8, height: 8).padding(.top, 6)
                VStack(alignment: .leading, spacing: 3) {
                    HomeMixedText.make(item.title, size: 15, weight: 600, color: .cavnarInk)
                    if let detail = item.detail {
                        HomeMixedText.make(detail, size: 12.5, weight: 500, color: .cavnarInk3)
                    }
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 6) {
                    if let action = item.action {
                        Button {
                            Haptic.light()
                            if action.route?.mobile != nil {
                                Task { await viewModel.run(item) }
                            } else if let module = action.module {
                                onOpenModule(module)
                            }
                        } label: {
                            Text(action.label)
                                .font(.cavnarBody(13, weight: 600))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .buttonStyle(.plain)
                    }
                    Button {
                        Haptic.light()
                        Task { await viewModel.snooze(item) }
                    } label: {
                        Text("Not today")
                            .font(.cavnarBody(12.5))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Puts it back tomorrow")
                }
            }
            .padding(.vertical, 11)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }

    private func lineRow(_ text: String, tone: Color, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Circle().fill(tone).frame(width: 8, height: 8).padding(.top, 6)
                HomeMixedText.make(text, size: 14.5, weight: 500, color: .cavnarInk2)
                Spacer(minLength: 0)
            }
            .padding(.vertical, 11)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }

}

// MARK: - The handoff itself

private enum CloseOutField: Hashable, CaseIterable {
    case well, wrong, eightySix, callouts
    case equipment, vips, maintenance, shiftNotes, generalNotes, influence
}

struct CloseOutSheet: View {
    let viewModel: HomeFollowThroughViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var draft = CloseOutDraft()
    @State private var postedLabel: String?
    @FocusState private var focused: CloseOutField?

    private var canSubmit: Bool { !draft.isEmpty }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("The numbers from tonight land at 3am. This is the only account of what "
                         + "actually happened — it leads tomorrow morning's brief.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    AccountSection(kicker: viewModel.closeOutDate ?? "Tonight") {
                        AccountField(label: "What went well", text: $draft.wentWell,
                                     focus: $focused, field: CloseOutField.well)
                        AccountField(label: "What went wrong", text: $draft.wentWrong,
                                     focus: $focused, field: CloseOutField.wrong)
                        AccountField(label: "What we ran out of", text: $draft.eightySixed,
                                     focus: $focused, field: CloseOutField.eightySix)
                        AccountField(label: "Who didn't make it", text: $draft.callouts,
                                     focus: $focused, field: CloseOutField.callouts,
                                     showsDivider: false)
                    }

                    // The six the nightly report adds. All optional; the
                    // manager's words go into the report exactly as written.
                    AccountSection(kicker: "For the daily report") {
                        AccountField(label: "Equipment", text: $draft.equipment,
                                     focus: $focused, field: CloseOutField.equipment)
                        AccountField(label: "VIP guests", text: $draft.vipGuests,
                                     focus: $focused, field: CloseOutField.vips)
                        AccountField(label: "Maintenance", text: $draft.maintenance,
                                     focus: $focused, field: CloseOutField.maintenance)
                        AccountField(label: "Shift notes", text: $draft.shiftNotes,
                                     focus: $focused, field: CloseOutField.shiftNotes)
                        AccountField(label: "General notes", text: $draft.generalNotes,
                                     focus: $focused, field: CloseOutField.generalNotes)
                        AccountField(label: "Influence — what was going on (Cubs, Bears, an event)", text: $draft.influence,
                                     focus: $focused, field: CloseOutField.influence,
                                     showsDivider: false)
                    }

                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        Task {
                            if await viewModel.saveCloseOut(draft) {
                                Haptic.success()
                                postedLabel = "Handed off"
                            }
                        }
                    } label: {
                        Text("Hand off the night").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                    .disabled(!canSubmit)
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .accountSheetChrome("Close-out")
            .keyboardNavToolbar($focused)
            .onAppear {
                guard let c = viewModel.closeOut else { return }
                draft = CloseOutDraft(c)
            }
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}
