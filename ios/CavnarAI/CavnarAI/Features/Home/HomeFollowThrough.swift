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

/// One "Still open" item (GET /mobile/api/actions, action_queue). Its action
/// is decoded whole (parity audit #3): the body the route needs is passed
/// through as the server wrote it — it decoded only a reprice's dish and
/// price, so Send now posted `{}` where publish reads `schedule_id`, Approve
/// posted `{}` where decide reads `decision`, and both got a 400 that a
/// `try?` swallowed. `confirm` (an outward action's confirm card), `alt`
/// (Deny beside Approve) and `nav` (the item itself) were dropped too.
struct ActionItem: Decodable, Identifiable {
    /// The same endpoint on each client — the web and mobile APIs have
    /// different prefixes, so the server hands over both.
    struct Route: Decodable, Equatable { let web: String?; let mobile: String? }

    /// The command action whose confirm card an outward step opens first
    /// (POST /command/propose) — an answer reaching an employee, the week
    /// going to staff (DESIGN_SYSTEM §10, re-audit F1-3).
    struct Confirm: Decodable, Equatable {
        let action: String
        let args: [String: AnyCodableValue]

        enum CodingKeys: String, CodingKey { case action, args }

        init(action: String, args: [String: AnyCodableValue] = [:]) {
            self.action = action; self.args = args
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            action = try c.decode(String.self, forKey: .action)
            args = ((try? c.decodeIfPresent([String: AnyCodableValue].self, forKey: .args)) ?? nil) ?? [:]
        }
    }

    /// One thing the row can do: its primary action, or the `alt` beside it.
    struct Step: Decodable, Equatable {
        let label: String
        let route: Route?
        let method: String?
        let module: String?
        /// What the route needs posted, exactly as the server wrote it.
        let body: [String: AnyCodableValue]?
        let confirm: Confirm?
        let nav: String?
        /// "Open it" on an unanswered Ask proposal carries the question too.
        let ask: String?

        enum CodingKeys: String, CodingKey { case label, route, method, module, body, confirm, nav, ask }

        init(label: String, route: Route? = nil, method: String? = nil, module: String? = nil,
             body: [String: AnyCodableValue]? = nil, confirm: Confirm? = nil, nav: String? = nil,
             ask: String? = nil) {
            self.label = label; self.route = route; self.method = method; self.module = module
            self.body = body; self.confirm = confirm; self.nav = nav; self.ask = ask
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            label = ((try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil) ?? "Open"
            route = (try? c.decodeIfPresent(Route.self, forKey: .route)) ?? nil
            method = (try? c.decodeIfPresent(String.self, forKey: .method)) ?? nil
            module = (try? c.decodeIfPresent(String.self, forKey: .module)) ?? nil
            body = (try? c.decodeIfPresent([String: AnyCodableValue].self, forKey: .body)) ?? nil
            confirm = (try? c.decodeIfPresent(Confirm.self, forKey: .confirm)) ?? nil
            nav = (try? c.decodeIfPresent(String.self, forKey: .nav)) ?? nil
            ask = (try? c.decodeIfPresent(String.self, forKey: .ask)) ?? nil
        }

        /// What a tap on this step does, in the web's order (hbQueueActs):
        /// a confirm card first, else the route in place, else the item's
        /// place, else Ask, else the module.
        enum Kind: Equatable { case confirm(Confirm), post(String), open, ask(String), module(String), none }
        var kind: Kind {
            if let confirm { return .confirm(confirm) }
            if let path = route?.mobile, !path.isEmpty { return .post(path) }
            if nav != nil { return .open }
            if let ask, !ask.isEmpty { return .ask(ask) }
            if let module { return .module(module) }
            return .none
        }
    }

    /// The row's action: a Step, plus the `alt` beside it.
    struct Action: Decodable {
        let step: Step
        let alt: Step?
        var label: String { step.label }
        var route: Route? { step.route }
        var module: String? { step.module }
        var body: [String: AnyCodableValue]? { step.body }
        var confirm: Confirm? { step.confirm }
        var nav: String? { step.nav }

        enum CodingKeys: String, CodingKey { case alt }

        init(step: Step, alt: Step? = nil) { self.step = step; self.alt = alt }

        init(from decoder: Decoder) throws {
            step = try Step(from: decoder)
            let c = try decoder.container(keyedBy: CodingKeys.self)
            alt = (try? c.decodeIfPresent(Step.self, forKey: .alt)) ?? nil
        }
    }

    let key: String
    let kind: String
    let title: String
    let detail: String?
    let severity: String
    let action: Action?
    /// Where the item itself opens (action_queue.nav_for).
    let nav: String?
    /// How many the row stands for (the drafted replies waiting) — what
    /// the widget reads from Home's own read (HomeReadShare).
    var count: Int? = nil
    var id: String { key }

    enum CodingKeys: String, CodingKey { case key, kind, title, detail, severity, action, nav }

    init(key: String, kind: String, title: String, detail: String? = nil, severity: String = "watch",
         action: Action? = nil, nav: String? = nil) {
        self.key = key; self.kind = kind; self.title = title; self.detail = detail
        self.severity = severity; self.action = action; self.nav = nav
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        kind = ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil) ?? ""
        title = try c.decode(String.self, forKey: .title)
        detail = (try? c.decodeIfPresent(String.self, forKey: .detail)) ?? nil
        severity = ((try? c.decodeIfPresent(String.self, forKey: .severity)) ?? nil) ?? "watch"
        action = (try? c.decodeIfPresent(Action.self, forKey: .action)) ?? nil
        nav = (try? c.decodeIfPresent(String.self, forKey: .nav)) ?? nil
    }

    /// The stored Ask proposal this row is, for key "ask:<id>".
    var proposalId: Int? {
        let parts = key.split(separator: ":", maxSplits: 1)
        guard parts.count == 2, parts[0] == "ask", let id = Int(parts[1]), id > 0 else { return nil }
        return id
    }

    /// Where a tap on the row lands: an unanswered Ask proposal opens as a
    /// proposal — `action/<id>` is a queued send's address, a different id
    /// space (F3-2) — and "Open it" on one used to push the "ask" module, a
    /// Coming soon screen; else the item's own nav, else the action's.
    var destination: NavPath? {
        if let id = proposalId { return NavPath("proposal/\(id)") }
        return NavPath(nav) ?? NavPath(action?.nav) ?? action?.module.flatMap { NavPath($0) }
    }

    /// Where a step with no route or confirm opens: the row's destination
    /// when the step is the row's own "Open it", else the step's nav.
    func destination(for step: Step) -> NavPath? {
        if proposalId != nil { return destination }
        return NavPath(step.nav) ?? destination
    }

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
        // F10: over the line but inside its normal week-to-week movement —
        // not yet a clear hit, so neutral, never a met goal's green.
        case "at_target": return .cavnarInk2
        default: return .cavnarInk3
        }
    }

    /// The state in words when the server's summary is absent — "at the
    /// target, within normal movement" for F10's new state.
    var stateLabel: String? {
        switch state {
        case "at_target": return "at the target, within its normal movement \u{2014} not yet a clear hit"
        default: return nil
        }
    }
}

/// A goal a teammate (or the sales audit) proposed, waiting for an account
/// holder (memory round 9/29/26, M2 owner_goals — GET /goals `proposed`).
/// Once confirmed it is the target every module judges its metric against.
struct ProposedGoal: Codable, Identifiable, Hashable {
    let id: Int
    let summary: String
    var proposedBy: String? = nil

    enum CodingKeys: String, CodingKey {
        case id, summary, label
        case proposedBy = "proposed_by"
    }

    init(id: Int, summary: String, proposedBy: String? = nil) {
        self.id = id; self.summary = summary; self.proposedBy = proposedBy
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        let text = ((try? c.decodeIfPresent(String.self, forKey: .summary)) ?? nil)
            ?? ((try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil)
        guard let t = text?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty else {
            throw DecodingError.dataCorruptedError(forKey: .summary, in: c, debugDescription: "no summary")
        }
        summary = t
        let by = ((try? c.decodeIfPresent(String.self, forKey: .proposedBy)) ?? nil)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        proposedBy = (by?.isEmpty ?? true) ? nil : by
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(summary, forKey: .summary)
        try c.encodeIfPresent(proposedBy, forKey: .proposedBy)
    }

    /// "Proposed by Dana, manager" / "Proposed by your sales audit".
    var byLine: String {
        guard let by = proposedBy else { return "Proposed by a teammate" }
        return "Proposed by " + (by == "Your sales audit" ? "your sales audit" : by)
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
    /// Goals waiting for an account holder, and whether this login is one.
    var proposedGoals: [ProposedGoal] = []
    /// Missed goals waiting to be renewed or closed (parity audit #66).
    var missedGoals: [ProposedGoal] = []
    var canConfirmGoals = false
    /// The goal being confirmed or declined right now.
    var answeringGoal: Int?
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
    /// "Staff rated tonight 4.2 out of 5 (6 answers)." — only past the floor.
    var closeOutStaffPulse: CloseOutStaffPulse?
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
    /// True once the day's reads (the cross-module finding among them) have
    /// landed at least once — until then Home doesn't know its one thing and
    /// promotes nothing (the web's `renderFocusPending`).
    private(set) var crossLoaded = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// Every cross-module link past the one the one-thing card leads with,
    /// as a Needs-attention row (density fix #5, web `hbLinkRows`): What
    /// connects is gone from Home, and a link is a decision — Done / Not for
    /// us — so it never hides in a collapsed section. Its Evidence opens the
    /// same "To confirm" and "Could also be" (HomeLinkEvidenceSheet).
    var linkItems: [NeedsAttentionItem] {
        links.enumerated().map { i, l in
            NeedsAttentionItem(
                type: Self.linkType(l.recKey ?? "\(i)"), module: l.modules?.first ?? "home",
                title: l.headline,
                // How long it has stood, beside the modules (memory round:
                // "Found 3 weeks running, since 9/7/26").
                detail: ([(l.modules ?? []).map { RecSummaryFormat.moduleLabel($0) }.joined(separator: " + ")]
                         + [l.memory?.line].compactMap { $0 })
                    .filter { !$0.isEmpty }.joined(separator: " \u{00B7} "),
                cta: "Evidence", secondary: nil, action: "link_evidence",
                recKey: l.recKey, dismissable: false, timesHidden: nil, count: nil,
                evidence: l.evidence?.first, confidence: nil)
        }
    }

    static func linkType(_ key: String) -> String { "link:" + key }

    /// The link a Needs-attention row stands for (its `type`).
    func link(for item: NeedsAttentionItem) -> CrossModule.Link? {
        links.enumerated().first { i, l in Self.linkType(l.recKey ?? "\(i)") == item.type }?.element
    }

    /// Results that have landed and wait on the owner's check-in (#21) —
    /// at most two on Home; the rest wait in the recommendation record.
    var checkInsDue: [RecOutcome] { Array(outcomes.filter(RecCheckIn.isDue).prefix(2)) }

    private struct ActionsResponse: Decodable { let ok: Bool; let items: [ActionItem] }
    struct GoalsResponse: Decodable {
        let ok: Bool
        let goals: [GoalRow]
        var proposed: HomeLenientList<ProposedGoal>? = nil
        /// Goals whose date passed without reaching them, retired from every
        /// prompt and asked once: renew or close (memory re-audit R3,
        /// parity audit #66). Same {id, summary} shape as a proposal.
        var missed: HomeLenientList<ProposedGoal>? = nil
        var canConfirm: Bool? = nil
        enum CodingKeys: String, CodingKey {
            case ok, goals, proposed, missed
            case canConfirm = "can_confirm"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
            goals = (try? c.decode([GoalRow].self, forKey: .goals)) ?? []
            proposed = (try? c.decodeIfPresent(HomeLenientList<ProposedGoal>.self, forKey: .proposed)) ?? nil
            missed = (try? c.decodeIfPresent(HomeLenientList<ProposedGoal>.self, forKey: .missed)) ?? nil
            canConfirm = (try? c.decodeIfPresent(Bool.self, forKey: .canConfirm)) ?? nil
        }
    }
    private struct CloseOutResponse: Decodable {
        let ok: Bool
        let closeout: CloseOutEntry?
        let businessDate: String?
        /// How tonight felt to staff, once enough answered (parity #67).
        var staffPulse: CloseOutStaffPulse? = nil
        enum CodingKeys: String, CodingKey {
            case ok, closeout
            case businessDate = "business_date"
            case staffPulse = "staff_pulse"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = try c.decode(Bool.self, forKey: .ok)
            closeout = try c.decodeIfPresent(CloseOutEntry.self, forKey: .closeout)
            businessDate = try c.decodeIfPresent(String.self, forKey: .businessDate)
            staffPulse = (try? c.decodeIfPresent(CloseOutStaffPulse.self, forKey: .staffPulse)) ?? nil
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
            /// A measured sales rise: revenue, not profit — kept apart from
            /// the savings and never added to them (re-audit A6).
            let salesLift: SalesLift?
            /// F4 (CA2 #7): the improvements by attribution grade — clear or
            /// held moves apart from ones tied to other changes (or past the
            /// band only once). PARTS of `monthly`, shown side by side and
            /// never added to it or to anything else. I7: `scope` says what
            /// kind of figure `monthly` is ("monthly_rate").
            var consistentMonthly: Double? = nil
            var associatedMonthly: Double? = nil
            var scope: String? = nil
            struct SalesLift: Decodable {
                let monthly: Double?
                let wins: Int?
                init(from decoder: Decoder) throws {
                    let c = try decoder.container(keyedBy: CodingKeys.self)
                    monthly = try? c.decodeIfPresent(Double.self, forKey: .monthly)
                    wins = try? c.decodeIfPresent(Int.self, forKey: .wins)
                }
                enum CodingKeys: String, CodingKey { case monthly, wins }
            }
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
                case salesLift = "sales_lift"
                case consistentMonthly = "consistent_monthly"
                case associatedMonthly = "associated_monthly"
                case scope
            }
        }
        /// I7: the headings the four figures sit under — "What was measured"
        /// over `delivered` only, then "What Cavnar surfaced / still
        /// available" (value_delivered.VALUE_SECTIONS). Absent on an older
        /// server, which keeps the built-in headings.
        struct Section: Decodable {
            let key: String?
            let heading: String?
            let figures: [String]?
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
        var sections: [Section]? = nil

        /// The heading the server gives a section ("measured" / "surfaced"),
        /// else `fallback`.
        func heading(_ key: String, fallback: String) -> String {
            guard let h = sections?.first(where: { $0.key == key })?.heading?
                .trimmingCharacters(in: .whitespaces), !h.isEmpty else { return fallback }
            return h
        }

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
            /// How long the link has stood (memory round, 9/29/26:
            /// link_memory) — first and last found, weeks running, whether
            /// it came back after an answer, and the server's sentence.
            /// Nil on an older server; an unknown `kind` is kept as text.
            var memory: LinkMemory? = nil
            var id: String { headline }
            enum CodingKeys: String, CodingKey {
                case kind, headline, modules, evidence, alternative, ask, answerable, memory
                case confirmBy = "confirm_by"
                case notACause = "not_a_cause"
                case recKey = "rec_key"
            }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                headline = try c.decode(String.self, forKey: .headline)
                kind = try? c.decodeIfPresent(String.self, forKey: .kind)
                modules = try? c.decodeIfPresent([String].self, forKey: .modules)
                evidence = try? c.decodeIfPresent([String].self, forKey: .evidence)
                confirmBy = try? c.decodeIfPresent(String.self, forKey: .confirmBy)
                alternative = try? c.decodeIfPresent(String.self, forKey: .alternative)
                notACause = try? c.decodeIfPresent(String.self, forKey: .notACause)
                ask = try? c.decodeIfPresent(String.self, forKey: .ask)
                recKey = try? c.decodeIfPresent(String.self, forKey: .recKey)
                answerable = try? c.decodeIfPresent(Bool.self, forKey: .answerable)
                memory = (try? c.decodeIfPresent(LinkMemory.self, forKey: .memory)) ?? nil
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
            /// K1 — how sure, with "Why?" (K4). Absent on an older server.
            let confidence: TrustConfidence?
            /// measured / computed / forecast / inferred (K4).
            let claimKind: String?
            /// The money fallback as the RANGE it is (K4): never collapsed
            /// to one figure. Only drawn when `dollarsMonthly` is absent.
            let money: Money?
            /// Advice the hero pulls against, for the owner to settle
            /// (memory round 9/29/26, lever_conflicts). Absent on an older
            /// server.
            var conflict: RecConflict? = nil
            /// F6: the dollars corrected by measured results
            /// (RecDollarCalibration). Absent on an older server.
            var dollarsAdjusted: Double? = nil
            var calibrationN: Int? = nil
            var calibrationNote: String? = nil
            /// What the figure covers (dollars_basis, B4 H7): the whole
            /// schedule's gap, one driver alone …
            var dollarsBasis: String? = nil
            /// A link found weeks running, or back after an answer, is
            /// escalated (memory round: `recurring`, `link_memory`); its
            /// `why` already carries the memory's sentence.
            var recurring: Bool? = nil
            var linkMemory: LinkMemory? = nil
            /// The monthly figure the hero states — calibrated when sent.
            var statedDollars: Double? { RecDollarCalibration.figure(raw: dollarsMonthly, adjusted: dollarsAdjusted) }
            var dollarsNote: String? {
                RecDollarCalibration.note(adjusted: dollarsAdjusted, n: calibrationN, note: calibrationNote)
            }
            struct Money: Decodable, Equatable {
                let low: Double?
                let high: Double?
                let label: String?
                init(from decoder: Decoder) throws {
                    let c = try decoder.container(keyedBy: CodingKeys.self)
                    low = try? c.decodeIfPresent(Double.self, forKey: .low)
                    high = try? c.decodeIfPresent(Double.self, forKey: .high)
                    label = try? c.decodeIfPresent(String.self, forKey: .label)
                }
                enum CodingKeys: String, CodingKey { case low, high, label }
                /// "$1,200–$2,400/month" — both ends, or the one that exists;
                /// nil when neither is a positive figure.
                var rangeText: String? {
                    let l = (low ?? 0) > 0 ? low : nil
                    let h = (high ?? 0) > 0 ? high : nil
                    switch (l, h) {
                    case let (l?, h?) where l.rounded() != h.rounded():
                        return "$\(l.commaFormatted)\u{2013}$\(h.commaFormatted)/month"
                    case let (l?, _): return "$\(l.commaFormatted)/month"
                    case let (nil, h?): return "up to $\(h.commaFormatted)/month"
                    default: return nil
                    }
                }
            }
            enum CodingKeys: String, CodingKey {
                case key, what, why, modules, evidence, alternative, answerable, confidence, money, conflict
                case dollarsMonthly = "dollars_monthly"
                case confirmBy = "confirm_by"
                case linkHeadline = "link_headline"
                case recKey = "rec_key"
                case claimKind = "claim_kind"
                case dollarsAdjusted = "dollars_adjusted"
                case calibrationN = "calibration_n"
                case calibrationNote = "calibration_note"
                case dollarsBasis = "dollars_basis"
                case recurring
                case linkMemory = "link_memory"
            }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                conflict = (try? c.decodeIfPresent(RecConflict.self, forKey: .conflict)) ?? nil
                dollarsBasis = RecDollarCalibration.basis((try? c.decodeIfPresent(String.self, forKey: .dollarsBasis)) ?? nil)
                dollarsAdjusted = try? c.decodeIfPresent(Double.self, forKey: .dollarsAdjusted)
                calibrationN = try? c.decodeIfPresent(Int.self, forKey: .calibrationN)
                calibrationNote = try? c.decodeIfPresent(String.self, forKey: .calibrationNote)
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
                confidence = try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence)
                claimKind = try? c.decodeIfPresent(String.self, forKey: .claimKind)
                money = try? c.decodeIfPresent(Money.self, forKey: .money)
                recurring = (try? c.decodeIfPresent(Bool.self, forKey: .recurring)) ?? nil
                linkMemory = (try? c.decodeIfPresent(LinkMemory.self, forKey: .linkMemory)) ?? nil
            }
            /// The key its answer row posts — rec_key, else the pick's own key.
            var answerKey: String? { recKey ?? key }
            /// The range to draw when there is no measured monthly figure.
            var moneyRange: String? {
                guard (dollarsMonthly ?? 0) <= 0 else { return nil }
                return money?.rangeText
            }
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
    /// The work half's reads — what Home's first screens show. The Results
    /// blocks below the fold (what worked, the value figures, what got
    /// better, last month) are read by `loadResults()` when Results scrolls
    /// into view (parity audit #20: eleven requests went out at once on
    /// every cold launch, four of them for a section that starts closed).
    func load() async {
        isLoading = actions.isEmpty && goals.isEmpty && results.isEmpty
        defer { isLoading = false }
        // The widget refresh borrows this read rather than making its own
        // at the same moment (HomeReadShare).
        HomeReadShare.shared.beginActions()
        async let a: ActionsResponse? = try? client.send("/mobile/api/actions", hapticOnError: false)
        async let g: GoalsResponse? = try? client.send("/mobile/api/goals", hapticOnError: false)
        async let o: RecOutcomesResponse? = try? client.send("/mobile/api/outcomes", hapticOnError: false)
        async let c: CloseOutResponse? = try? client.send("/mobile/api/closeout", hapticOnError: false)
        async let x: CrossModule? = try? client.send("/mobile/api/cross-module", hapticOnError: false)
        async let ms: Milestones? = try? client.send("/mobile/api/milestones", hapticOnError: false)
        async let ls: LossSignals? = try? client.send("/mobile/api/loss-signals", hapticOnError: false)
        // Once Results has been read, a reload keeps it current too.
        async let lazy: Void = refreshResultsIfLoaded()
        let actionsRead = await a
        actions = actionsRead?.items ?? []
        HomeReadShare.shared.finishActions(actionsRead.map { $0.items.map { (key: $0.key, count: $0.count) } })
        let goalsResponse = await g
        goals = goalsResponse?.goals ?? []
        proposedGoals = goalsResponse?.proposed?.items ?? []
        missedGoals = goalsResponse?.missed?.items ?? []
        canConfirmGoals = goalsResponse?.canConfirm ?? false
        let fetchedOutcomes = await o
        outcomes = fetchedOutcomes?.outcomes ?? []
        results = outcomes.filter { $0.summary?.isEmpty == false }
        caveat = fetchedOutcomes?.caveat
        let close = await c
        closeOut = close?.closeout
        closeOutDate = close?.businessDate
        closeOutStaffPulse = close?.staffPulse
        let cross = await x
        fixFirst = cross?.fixFirst.flatMap { ($0.what ?? "").isEmpty ? nil : $0 }
        // The one thing owns a link it leads with — the same finding is
        // never on the page twice (the web's renderConnections rule).
        let owned = fixFirst.flatMap { $0.linkHeadline ?? $0.what }
        links = (cross?.links ?? []).filter { $0.headline != owned }
        crossLoaded = true
        // Only ever offer one, and only one that has not been shown on any
        // device — the server's UNIQUE row is what makes that true.
        pendingMilestone = (await ms)?.unseen?.first
        let loss = await ls
        lossFlags = (loss?.available == true) ? (loss?.flagged ?? []) : []
        lossWeek = loss?.week ?? []
        lossNote = loss?.note
        _ = await lazy
    }

    /// True once the Results blocks have been read (and are then kept
    /// current by every `load()`).
    private(set) var resultsLoaded = false
    private var resultsInFlight = false

    private func refreshResultsIfLoaded() async {
        if resultsLoaded { await loadResults() }
    }

    /// The Results blocks: what worked, the value figures, what got better
    /// and last month — read the first time Results scrolls into view (or
    /// the measured-results sheet opens), not at launch.
    func loadResults() async {
        guard !resultsInFlight else { return }
        resultsInFlight = true
        defer { resultsInFlight = false }
        // 180 days: the server's default window and the monthly email's,
        // so Home says the same sentences the owner was emailed.
        async let ww: WhatWorked? = try? client.send("/mobile/api/recs/what-worked", query: ["days": "180"],
                                                     hapticOnError: false)
        async let v: ValueSummary? = try? client.send("/mobile/api/value", hapticOnError: false)
        async let n: GoodNews? = try? client.send("/mobile/api/good-news", hapticOnError: false)
        async let mr: HomeMonthlyReview? = try? client.send("/mobile/api/monthly-review", hapticOnError: false)
        whatWorked = await ww
        value = await v
        let news = await n
        goodNews = news?.items ?? []
        goodNewsCaveat = news?.caveat
        month = await mr
        resultsLoaded = true
    }

    /// Renew a missed goal for another month (the same target, a new date)
    /// or close it for good — POST /mobile/api/goals/<id>/renew | close, the
    /// web Goals card's two buttons (parity audit #66). Returns the line to
    /// show, or nil (`missedGoalError` says why).
    func answerMissed(_ goal: ProposedGoal, renew: Bool) async -> String? {
        struct RenewBody: Encodable { let days: Int }
        guard answeringGoal == nil else { return nil }
        answeringGoal = goal.id
        defer { answeringGoal = nil }
        missedGoalError = nil
        do {
            let path = "/mobile/api/goals/\(goal.id)/\(renew ? "renew" : "close")"
            let r: OKResponse = renew
                ? try await client.send(path, method: .post, body: RenewBody(days: 30), retryTransient: false)
                : try await client.send(path, method: .post, body: [String: String](), retryTransient: false)
            guard r.ok else { missedGoalError = r.error ?? "Couldn\u{2019}t save that."; return nil }
            await Haptic.success()
            missedGoals.removeAll { $0.id == goal.id }
            await load()
            return renew ? "Renewed for another month \u{2014} it\u{2019}s the target again" : "Closed"
        } catch let error as APIClient.APIError {
            missedGoalError = error.message
            return nil
        } catch {
            missedGoalError = "Couldn\u{2019}t save that."
            return nil
        }
    }

    /// Why the last renew / close of a missed goal did not save.
    var missedGoalError: String?

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
            /// What the answer does, in the server's words (rec_ledger.
            /// silence_message — "Noted — Cavnar AI will bring it back in
            /// 4 weeks", "hidden for you; the owner still sees it").
            var message: String? = nil
            enum CodingKeys: String, CodingKey {
                case ok, outcome, error, tracker, message
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
            return Self.answerLine(kind: kind, message: r.message,
                                   trackerLine: RecTrackerNote.line(tracker: r.tracker, refused: r.trackerRefused),
                                   evaluateOn: r.outcome?.evaluateOn)
        } catch { errorMessage = "Couldn\u{2019}t save that."; return nil }
    }

    /// The line an answer leaves on Home: the server's own sentence for what
    /// the answer does (memory round 9/29/26 — it holds 60 days, a year,
    /// until the next count …), then, on a Done, what it started measuring.
    /// The local words are only for an older server that sends no message.
    static func answerLine(kind: String, message: String?, trackerLine: String?, evaluateOn: String?) -> String {
        let said = message?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let said, !said.isEmpty {
            guard kind == "done" else { return said }
            if let line = trackerLine { return said + ". " + line }
            if let on = evaluateOn { return said + ". Measuring from today, result on \(CavnarDate.mdy(on))" }
            return said
        }
        if kind == "done" {
            // What Done started measuring, or why nothing did (another
            // change already on the same number).
            if let line = trackerLine {
                return "Marked done \u{2014} " + line.prefix(1).lowercased() + line.dropFirst()
            }
            if let on = evaluateOn {
                return "Marked done — measuring from today, result on \(CavnarDate.mdy(on))"
            }
        }
        switch kind {
        case "done": return "Marked done"
        case "recommendation": return "Hidden for two weeks"
        case "snooze": return "Not today \u{2014} it\u{2019}s back tomorrow"
        default: return "Noted — it won\u{2019}t come back"
        }
    }

    /// Not today (kind "snooze") or hide (kind "recommendation") on a
    /// Needs-attention item — the same answer, through the same route, as a
    /// recommendation. Never offered for a critical item. A second hide
    /// asks why, and that answer arrives as not_for_us with its reason code.
    /// Returns the line to show — the server's `message` for what the
    /// answer does, else the local words — or nil when it did not save.
    @discardableResult
    func answerAttention(_ item: NeedsAttentionItem, kind: String, reason: String? = nil,
                         reasonCode: String? = nil) async -> String? {
        struct Resp: Decodable { let ok: Bool; let message: String? }
        let r: Resp? = try? await client.send(
            "/mobile/api/home/dismiss", method: .post,
            body: DismissBody(key: item.recKey ?? item.type, kind: kind,
                              title: (reason != nil || reasonCode != nil) ? item.title : nil, metric: nil,
                              reason: reason, reasonCode: reasonCode),
            retryTransient: false)
        guard r?.ok == true else { return nil }
        await Haptic.success()
        return Self.answerLine(kind: kind, message: r?.message, trackerLine: nil, evaluateOn: nil)
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

    /// Confirm (it becomes the active goal and the target) or decline a
    /// proposed goal — POST /mobile/api/goals/<id>/confirm | decline.
    /// Returns the line to show, or nil (errorMessage says why).
    func answerGoal(_ goal: ProposedGoal, confirm: Bool) async -> String? {
        guard answeringGoal == nil else { return nil }
        answeringGoal = goal.id
        defer { answeringGoal = nil }
        do {
            let r: OKResponse = try await client.send(
                "/mobile/api/goals/\(goal.id)/\(confirm ? "confirm" : "decline")", method: .post,
                body: [String: String](), retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that."; return nil }
            await Haptic.success()
            proposedGoals.removeAll { $0.id == goal.id }
            await load()
            return confirm ? "Confirmed \u{2014} it\u{2019}s the target from now on" : "Declined"
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return nil
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
            return nil
        }
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

    /// What one step of a Still-open row came to.
    enum StepOutcome: Equatable {
        /// It went through; the route's own warning beside it, if any.
        case done(warning: String?)
        /// The publish gate (409 needs_ack): what stops it, to be read
        /// before "Send anyway".
        case needsAck(texts: [String], acknowledgement: AnyCodableValue)
        /// It did not — the server's own sentence.
        case failed(String)
    }

    private struct StepResult: Decodable {
        let ok: Bool
        let error: String?
        let warning: String?
        let needsAck: Bool?
        let queued: Bool?
        enum CodingKeys: String, CodingKey {
            case ok, error, warning, queued
            case needsAck = "needs_ack"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = ((try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? nil) ?? false
            error = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
            warning = (try? c.decodeIfPresent(String.self, forKey: .warning)) ?? nil
            needsAck = (try? c.decodeIfPresent(Bool.self, forKey: .needsAck)) ?? nil
            queued = (try? c.decodeIfPresent(Bool.self, forKey: .queued)) ?? nil
        }
    }

    /// The body a step posts: the server's own body, whole, plus the
    /// acknowledgement when the owner sends past the publish gate.
    static func postBody(_ step: ActionItem.Step, acknowledge: AnyCodableValue? = nil) -> [String: AnyCodableValue] {
        var body = step.body ?? [:]
        if let acknowledge { body["acknowledge"] = acknowledge }
        return body
    }

    /// One row's last outcome, said under the row ("Approved", the gate's
    /// list, the server's refusal) — never swallowed (parity audit #3).
    var rowNote: [String: RowNote] = [:]
    /// The step being run, per row, while it is in flight.
    var running: Set<String> = []

    struct RowNote: Equatable {
        enum Tone: Equatable { case good, warn, bad }
        let text: String
        let tone: Tone
        /// The gate's blockers, and what "Send anyway" posts back.
        var blockers: [String] = []
        var ackStep: ActionItem.Step? = nil
        var acknowledgement: AnyCodableValue? = nil
    }

    /// POST a step's route with the server's body (an internal, reversible
    /// step — Reprice; an outward one goes through its confirm card first).
    /// The row says what happened; Still open re-reads on success.
    @discardableResult
    func run(_ item: ActionItem, step: ActionItem.Step, acknowledge: AnyCodableValue? = nil) async -> StepOutcome {
        guard let route = step.route?.mobile, !route.isEmpty else { return .failed("Nothing to run for that.") }
        running.insert(item.key)
        defer { running.remove(item.key) }
        let outcome: StepOutcome
        do {
            let r: StepResult = try await client.send(route, method: .post,
                                                      body: Self.postBody(step, acknowledge: acknowledge),
                                                      retryTransient: false)
            if r.ok {
                outcome = .done(warning: r.warning)
            } else if r.needsAck == true {
                outcome = .failed(r.error ?? "This week has things to look at first.")
            } else {
                outcome = .failed(r.error ?? "That didn\u{2019}t go through.")
            }
        } catch let error as APIClient.APIError {
            if let gate = error.decodeBody(AskBlockers.Gate.self), gate.needsAck {
                outcome = .needsAck(texts: gate.texts, acknowledgement: gate.acknowledgement)
            } else {
                outcome = .failed(error.message)
            }
        } catch is CancellationError {
            return .failed("Cancelled.")
        } catch {
            outcome = .failed("Couldn\u{2019}t reach Cavnar AI \u{2014} check your connection.")
        }
        switch outcome {
        case .done(let warning):
            await Haptic.success()
            rowNote[item.key] = warning.map { RowNote(text: $0, tone: .warn) } ?? RowNote(text: "Done", tone: .good)
            await load()
        case .needsAck(let texts, let ack):
            Haptic.warning()
            rowNote[item.key] = RowNote(text: "Read before this goes out", tone: .warn, blockers: texts,
                                        ackStep: step, acknowledgement: ack)
        case .failed(let message):
            rowNote[item.key] = RowNote(text: message, tone: .bad)
        }
        return outcome
    }

    private struct ProposeBody: Encodable {
        let action: String
        let args: [String: AnyCodableValue]
    }

    /// The confirm card for an outward step (POST /mobile/api/command/
    /// propose, no model call) — the same card Ask and the command sheet
    /// render. Nil with the server's reason in `rowNote` when it can't.
    func propose(_ item: ActionItem, _ confirm: ActionItem.Confirm) async -> AskProposal? {
        do {
            let r: CommandProposeResponse = try await client.send(
                "/mobile/api/command/propose", method: .post,
                body: ProposeBody(action: confirm.action, args: confirm.args), retryTransient: false)
            if r.ok, let p = r.proposal { return p }
            rowNote[item.key] = RowNote(text: r.error ?? "Couldn\u{2019}t open that \u{2014} open the item instead.",
                                        tone: .bad)
        } catch let error as APIClient.APIError {
            rowNote[item.key] = RowNote(text: error.message, tone: .bad)
        } catch is CancellationError {
        } catch {
            rowNote[item.key] = RowNote(text: "Couldn\u{2019}t reach Cavnar AI \u{2014} check your connection.", tone: .bad)
        }
        return nil
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
    /// Which half of the follow-through this instance draws (density #4).
    /// `.work` stays open on Home: what is still open, check-ins that ask a
    /// question, the loss and cross-module findings (both carry Done / Not
    /// for us — DS §12 keeps decisions out of collapsed sections), and the
    /// close-out. `.results` is what was measured, drawn inside Home's one
    /// collapsed "Results" disclosure. `.worth` is the Worth card alone,
    /// which lives in the measured-results sheet now instead of restating
    /// the band on Home. `.all` is the old single stack.
    enum Part { case all, work, results, worth }

    let viewModel: HomeFollowThroughViewModel
    var part: Part = .all
    /// False when HomeView renders the close-out in the day's slot (after 8pm).
    var showsCloseOut: Bool = true
    /// When /mobile/api/home last answered (HomeViewModel.lastLoadedAt).
    /// The queue drops what Home already showed today, which it learns from
    /// Home's own fetch — so it is read only after that fetch, never
    /// against a cached summary painted first (the queue then repeated a
    /// needs-attention item on the same screen), and again after each one.
    var homeLoadedAt: Date? = nil
    var onOpenModule: (String) -> Void
    /// Where a Still-open row (and its "Open it") lands: the item itself,
    /// through Home's router — a proposal opens as a proposal, a request as
    /// the request (parity audit #3).
    var onOpenNav: (NavPath) -> Void = { _ in }

    /// The recommendation record (RecommendationHistoryView), opened from
    /// "What your changes did" and from the worth card.
    @State private var showingRecord = false
    /// The outward step whose confirm card is open (Send now, Approve,
    /// Deny) — the same card Ask and the command sheet render.
    @State private var proposing: HomeActionProposal?
    /// The row whose "Send anyway" asked for its own confirm.
    @State private var sendingAnyway: ActionItem?
    /// The monthly review opened whole, with its PDF share (parity #79).
    @State private var showingMonth = false

    private var hasAnything: Bool {
        !viewModel.actions.isEmpty || !viewModel.goals.isEmpty || !viewModel.results.isEmpty
            || !viewModel.proposedGoals.isEmpty
    }

    private var drawsWork: Bool { part == .all || part == .work }
    private var drawsResults: Bool { part == .all || part == .results }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if drawsWork {
                workSections
            }
            if drawsResults {
                resultSections
            }
            if part == .worth {
                valueCard
            }
        }
        .task(id: homeLoadedAt) {
            guard homeLoadedAt != nil else { return }
            await viewModel.load()
        }
        .task {
            // The measured-results sheet's Worth card reads the value
            // figures, which Home now reads only once Results is in view.
            if part == .worth, !viewModel.resultsLoaded { await viewModel.loadResults() }
        }
        .sheet(isPresented: $showingRecord, onDismiss: { Task { await viewModel.load() } }) {
            RecommendationHistoryView()
        }
        .sheet(item: $proposing, onDismiss: { Task { await viewModel.load() } }) { p in
            HomeActionProposalSheet(proposal: p, viewModel: viewModel)
        }
        .sheet(isPresented: $showingMonth) {
            if let month = viewModel.month {
                HomeMonthlyReviewSheet(month: month)
            }
        }
        .confirmationDialog(sendingAnyway.map { Self.sendAnywayTitle(viewModel.rowNote[$0.key]?.blockers.count ?? 0) } ?? "",
                            isPresented: Binding(get: { sendingAnyway != nil },
                                                 set: { if !$0 { sendingAnyway = nil } }),
                            titleVisibility: .visible, presenting: sendingAnyway) { item in
            Button("Send it anyway", role: .destructive) {
                guard let note = viewModel.rowNote[item.key], let step = note.ackStep else { return }
                Task { await viewModel.run(item, step: step, acknowledge: note.acknowledgement) }
            }
            Button("Not yet", role: .cancel) {}
        } message: { _ in
            Text("It goes out with the warnings above. Open the week instead to fix them first.")
        }
    }

    /// "9/21/26 – 9/27/26" (the web's mdy range); nil without both ends.
    static func lossWeekLabel(_ week: [String]) -> String? {
        guard week.count == 2, !week[0].isEmpty, !week[1].isEmpty else { return nil }
        return CavnarDate.mdyRange(week[0], week[1])
    }

    static func sendAnywayTitle(_ n: Int) -> String {
        "Send it with \(n) rule warning\(n == 1 ? "" : "s")?"
    }

    /// True when `.results` has anything to draw — HomeView hides the
    /// Results disclosure's body rows when not, and its closed row still
    /// says what was measured.
    static func hasResults(_ vm: HomeFollowThroughViewModel) -> Bool {
        !vm.goals.isEmpty || !vm.results.isEmpty || !vm.goodNews.isEmpty
            || vm.whatWorked != nil || vm.month != nil
    }

    /// The work half — always open on Home.
    @ViewBuilder
    private var workSections: some View {
        // Goals a teammate proposed wait for an account holder — a
        // decision, so with the work, never inside Results (M2).
        if !viewModel.proposedGoals.isEmpty {
            HomeProposedGoals(viewModel: viewModel)
        }
        // A goal whose date passed: renew or close — a decision, so with
        // the work too (parity audit #66).
        if !viewModel.missedGoals.isEmpty {
            HomeMissedGoals(viewModel: viewModel)
        }
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

        // A result that landed asks whether the owner made the change —
        // a question, so it stays with the work, never inside a collapsed
        // section.
        if part == .work {
            ForEach(viewModel.checkInsDue) { outcome in
                RecCheckInCard(outcome: outcome, surface: "home") { await viewModel.load() }
            }
        }

        // What connects is not drawn here any more (parity audit #5, as
        // the web removed it 9/25/26): the lead link is the one thing and
        // every other link is a Needs-attention row (linkItems) whose
        // Evidence opens HomeLinkEvidenceSheet.

        lossCard

        // Before 8pm the handoff sits here, at the end; after 8pm
        // HomeView puts it in the day's slot instead (HomeCloseOutCard).
        if showsCloseOut {
            HomeCloseOutCard(viewModel: viewModel)
        }
    }

    /// The measured half — inside Home's collapsed Results. What got
    /// better is one list with what the owner's changes did (one
    /// "measured" card, not two), and Worth moved to the results sheet.
    @ViewBuilder
    private var resultSections: some View {
        if !viewModel.goals.isEmpty {
            HomeSectionHeader(kicker: "Where you're heading", title: "Goals")
            VStack(spacing: 0) {
                ForEach(Array(viewModel.goals.enumerated()), id: \.element.id) { index, goal in
                    lineRow(goal.summary ?? [goal.label, goal.stateLabel].compactMap { $0 }.joined(separator: ": "),
                            tone: goal.tone,
                            showsDivider: index < viewModel.goals.count - 1)
                }
            }
            .cavnarCard()
        }

        if !viewModel.results.isEmpty || !viewModel.goodNews.isEmpty
            || (part == .all && !viewModel.checkInsDue.isEmpty) {
            HomeSectionHeader(kicker: "Measured", title: "What your changes did")
            if !viewModel.results.isEmpty || !viewModel.goodNews.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(viewModel.results.enumerated()), id: \.element.id) { index, r in
                        resultRow(r, showsDivider: index < viewModel.results.count - 1)
                    }
                    if let caveat = viewModel.caveat, !viewModel.results.isEmpty {
                        CavnarCaveat(title: "Before and after, not proof", detail: caveat)
                            .padding(.top, 10)
                    }
                    // What got better, merged in (density #4): the same
                    // measured story, one card instead of two.
                    goodNewsRows(leadsCard: viewModel.results.isEmpty)
                    if !viewModel.results.isEmpty {
                        recordLink.padding(.top, 10)
                    }
                }
                .cavnarCard()
            }
            if part == .all {
                ForEach(viewModel.checkInsDue) { outcome in
                    RecCheckInCard(outcome: outcome, surface: "home") { await viewModel.load() }
                }
            }
        }

        if let worked = viewModel.whatWorked {
            WhatWorkedCard(whatWorked: worked)
        }

        if part == .all {
            valueCard
        }

        if let month = viewModel.month {
            HomeMonthlyReviewCard(month: month, onOpen: { showingMonth = true })
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
                    // Shown, never counted, when its baseline overlapped
                    // the trigger; the grade; the band's false-alarm rate.
                    ForEach(r.measurementNotes, id: \.self) { note in
                        HomeMixedText.make(note + ".", size: 12.5, weight: 500,
                                           color: note.hasPrefix("Not counted") ? .cavnarAmber : .cavnarInk3)
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
    ///
    /// Drawn inside "What your changes did" (density #4) under an "Also
    /// got better" kicker — or leading that card when there is no result
    /// of the owner's own yet.
    @ViewBuilder
    private func goodNewsRows(leadsCard: Bool) -> some View {
        if !viewModel.goodNews.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                Text(leadsCard ? "WHAT GOT BETTER" : "ALSO GOT BETTER")
                    .font(.cavnarBody(CavnarType.kicker, weight: 700))
                    .tracking(1.4)
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.top, leadsCard ? 0 : 14)
                    .padding(.bottom, 2)
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
            // The week as the owner reads dates, M/D/YY — it showed the
            // server's ISO end date (parity audit #19).
            HomeSectionHeader(kicker: "Comps, voids and refunds", title: "Worth reviewing",
                              trailing: Self.lossWeekLabel(viewModel.lossWeek))
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

    /// What Cavnar AI has been worth. Four figures, never added together —
    /// a measurement, an estimate at stated rates, an alert total and a gap
    /// against target are not addends (see value_delivered.py). They sit
    /// under two headings (CA4 F6, J6): what was measured, then what Cavnar
    /// surfaced or is still available — so an estimate never reads as a
    /// measured result.
    @ViewBuilder
    private var valueCard: some View {
        if let v = viewModel.value, let d = v.delivered, v.showsWorthCard {
            HomeSectionHeader(kicker: "Worth", title: v.heading("measured", fallback: "What was measured"))
            VStack(alignment: .leading, spacing: 0) {
                let unpriced = RecValueFormat.unpricedWinLines(d)
                if (d.wins ?? 0) > 0 {
                    lineRow(Self.deliveredLine(d), tone: .cavnarGreen, showsDivider: true)
                    // The same improvements split by how clearly they can
                    // be read (F4) — parts of the figure above, never added
                    // to it.
                    if let split = RecValueFormat.gradeSplitLine(d) {
                        lineRow(split, tone: .cavnarInk3, showsDivider: true)
                    }
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
                if let lift = d.salesLift?.monthly, lift > 0 {
                    lineRow("Sales measured up about \(Self.money(lift))/month \u{2014} revenue, not profit, "
                            + "so it isn\u{2019}t added to the savings.", tone: .cavnarGreen, showsDivider: true)
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
                    lineRow(denom, tone: .cavnarInk3, showsDivider: false)
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
            surfacedCard(v)
        }
    }

    /// The other three figures, each on its own line and never added to the
    /// measured ones: work done at stated rates (each with its rate and
    /// basis), dollars the alerts carried, and the gap still available.
    @ViewBuilder
    private func surfacedCard(_ v: HomeFollowThroughViewModel.ValueSummary) -> some View {
        let avoided = v.avoided.flatMap { Self.avoidedLine($0) }
        let rates = v.avoided.map { Self.avoidedRateLines($0) } ?? []
        let surfaced = v.surfaced.flatMap { sf -> String? in
            guard let dollars = sf.dollars, dollars > 0 else { return nil }
            return "\(Self.money(dollars)) of problems put in front of you across "
                + "\(sf.alerts ?? 0) alerts in the last \(sf.days ?? 30) days"
        }
        let opportunity = v.opportunity?.monthly.flatMap { op -> String? in
            op > 0 ? "\(Self.money(op))/month still on the table \u{2014} available, not captured" : nil
        }
        if avoided != nil || surfaced != nil || opportunity != nil {
            HomeSectionHeader(kicker: "Not measured",
                              title: v.heading("surfaced", fallback: "What Cavnar AI surfaced / still available"))
            VStack(alignment: .leading, spacing: 0) {
                if let avoided {
                    lineRow(avoided, tone: .cavnarInk3, showsDivider: rates.isEmpty && (surfaced != nil || opportunity != nil))
                    if !rates.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(Array(rates.enumerated()), id: \.offset) { _, line in
                                HomeMixedText.make(line, size: 12.5, weight: 500, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(.leading, 20)
                        .padding(.bottom, 11)
                        if surfaced != nil || opportunity != nil {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        }
                    }
                }
                if let surfaced {
                    lineRow(surfaced, tone: .cavnarInk3, showsDivider: opportunity != nil)
                }
                if let opportunity {
                    // A gap, not a result — amber "watch", never the ember
                    // or a win's green (DESIGN_SYSTEM §9).
                    lineRow(opportunity, tone: .cavnarAmber, showsDivider: false)
                }
            }
            .cavnarCard()
        }
    }

    /// Each avoided figure's own rate and what it stands for —
    /// "412 review replies written: $3.50 each — what a reply service
    /// charges". value_delivered states the rate for every item.
    static func avoidedRateLines(_ av: HomeFollowThroughViewModel.ValueSummary.Avoided) -> [String] {
        (av.items ?? []).compactMap { item in
            guard let label = item.label, !label.isEmpty else { return nil }
            var s = label
            if let rate = item.rate, !rate.isEmpty { s += ": " + rate }
            if let basis = item.basis, !basis.isEmpty { s += " \u{2014} " + basis }
            return s == label ? nil : s
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
                // The audit estimated; it promised nothing (NS3 M12).
                Text("WHAT THE AUDIT ESTIMATED")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.4)
                    .foregroundStyle(Color.cavnarEmber2)
                HomeMixedText.make(
                    // audit_date arrives ISO; an owner reads M/D/YY (CA1 O16).
                    "At your audit on \(p.auditDate.map { CavnarDate.mdy($0) } ?? "sign-up") we estimated "
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

    /// One Still-open row (parity audit #3, the web's hbQueueActs): a tap
    /// on the row opens the item itself; its action button does the step —
    /// an outward one (the week to staff, an answer to an employee) opens
    /// its confirm card first, never a one-tap post; the `alt` beside it
    /// (Deny beside Approve) is a trailing swipe and a context-menu item.
    /// What happened is said under the row, never swallowed.
    private func actionRow(_ item: ActionItem, showsDivider: Bool) -> some View {
        let note = viewModel.rowNote[item.key]
        let busy = viewModel.running.contains(item.key)
        let alt = item.action?.alt
        return VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Circle().fill(item.tone).frame(width: 8, height: 8).padding(.top, 6)
                VStack(alignment: .leading, spacing: 3) {
                    HomeMixedText.make(item.title, size: 15, weight: 600, color: .cavnarInk)
                    if let detail = item.detail {
                        HomeMixedText.make(detail, size: 12.5, weight: 500, color: .cavnarInk3)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
                .onTapGesture {
                    guard let nav = item.destination else { return }
                    Haptic.light()
                    onOpenNav(nav)
                }
                .accessibilityAddTraits(item.destination != nil ? .isButton : [])
                .accessibilityHint(item.destination != nil ? "Opens it" : "")
                VStack(alignment: .trailing, spacing: 6) {
                    if let action = item.action {
                        Button {
                            Haptic.light()
                            perform(action.step, on: item)
                        } label: {
                            Text(action.label)
                                .font(.cavnarBody(13, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                                .frame(minHeight: 32)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(busy)
                    }
                    if let alt {
                        Button {
                            Haptic.light()
                            perform(alt, on: item)
                        } label: {
                            Text(alt.label)
                                .font(.cavnarBody(12.5, weight: 600))
                                .foregroundStyle(Color.cavnarInk2)
                                .frame(minHeight: 28)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(busy)
                    }
                    Button {
                        Haptic.light()
                        Task { await viewModel.snooze(item) }
                    } label: {
                        Text("Not today")
                            .font(.cavnarBody(12.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .frame(minHeight: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Puts it back tomorrow")
                }
            }
            .padding(.vertical, 11)
            if busy {
                CavnarSkeletonBar(height: 3).padding(.leading, 20).padding(.bottom, 8)
            }
            if let note {
                rowNoteView(item, note)
            }
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
        .homeSwipeAction(alt.map { a in
            HomeSwipeAction(label: a.label, systemImage: Self.altGlyph(a), tint: Self.altTint(a)) {
                perform(a, on: item)
            }
        })
        .contextMenu {
            if let nav = item.destination {
                Button { onOpenNav(nav) } label: { Label("Open it", systemImage: "arrow.up.right") }
            }
            if let action = item.action {
                Button { perform(action.step, on: item) } label: {
                    Label(action.label, systemImage: action.step.confirm != nil ? "checkmark.seal" : "bolt")
                }
            }
            if let alt {
                Button(role: Self.altTint(alt) == .cavnarRed ? .destructive : nil) { perform(alt, on: item) } label: {
                    Label(alt.label, systemImage: Self.altGlyph(alt))
                }
            }
            Button { Task { await viewModel.snooze(item) } } label: { Label("Not today", systemImage: "moon") }
        }
    }

    /// What a step does when tapped — the web's order: the confirm card,
    /// else the route in place, else the item, else Ask, else the module.
    private func perform(_ step: ActionItem.Step, on item: ActionItem) {
        switch step.kind {
        case .confirm(let confirm):
            proposing = HomeActionProposal(item: item, step: step, confirm: confirm)
        case .post:
            Task { await viewModel.run(item, step: step) }
        case .open:
            if let nav = item.destination(for: step) { onOpenNav(nav) }
        case .ask(let question):
            // An unanswered Ask proposal reopens as itself, not as a new
            // question about it.
            if let nav = item.proposalId != nil ? item.destination : SystemEntry.askPath(question) {
                onOpenNav(nav)
            }
        case .module(let module):
            if let nav = item.destination(for: step) { onOpenNav(nav) } else { onOpenModule(module) }
        case .none:
            if let nav = item.destination { onOpenNav(nav) }
        }
    }

    /// Deny reads red (an answer that turns someone down); any other alt is
    /// the quiet ink of a secondary choice.
    static func altTint(_ step: ActionItem.Step) -> Color {
        if case .string(let d)? = step.confirm?.args["decision"], d == "deny" { return .cavnarRed }
        if case .string(let d)? = step.body?["decision"], d == "deny" { return .cavnarRed }
        return .cavnarInk3
    }

    static func altGlyph(_ step: ActionItem.Step) -> String {
        altTint(step) == .cavnarRed ? "xmark" : "arrow.up.right"
    }

    /// Under a row: what happened, or the publish gate's list with the one
    /// button that sends past it knowingly (after its own confirm).
    @ViewBuilder
    private func rowNoteView(_ item: ActionItem, _ note: HomeFollowThroughViewModel.RowNote) -> some View {
        let color: Color = note.tone == .good ? .cavnarGreen : (note.tone == .warn ? .cavnarAmber : .cavnarRed)
        VStack(alignment: .leading, spacing: 6) {
            if note.blockers.isEmpty {
                HomeMixedText.make(note.text, size: 12.5, weight: 600, color: color)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(note.text.uppercased())
                    .font(.cavnarBody(CavnarType.kicker, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                ForEach(Array(note.blockers.enumerated()), id: \.offset) { _, b in
                    HStack(alignment: .top, spacing: 8) {
                        Circle().fill(Color.cavnarRed).frame(width: 6, height: 6).padding(.top, 6)
                        HomeMixedText.make(b, size: 13, weight: 500, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Button {
                    Haptic.warning()
                    sendingAnyway = item
                } label: {
                    Text(Self.sendAnywayLabel(note.blockers.count))
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .padding(.top, 2)
            }
        }
        .padding(.leading, 20)
        .padding(.bottom, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// "Send with 2 rule warnings…" — the web's hbAckInPlace button.
    static func sendAnywayLabel(_ n: Int) -> String {
        "Send with \(n) rule warning\(n == 1 ? "" : "s")\u{2026}"
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

                    // M/D/YY, never the server's ISO date (parity audit #19).
                    AccountSection(kicker: viewModel.closeOutDate.map { CavnarDate.mdy($0) } ?? "Tonight") {
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

/// "Goals waiting for you" — each proposed goal in its own words, who
/// proposed it, and Confirm / Decline for an account holder. A teammate
/// sees the same list read-only: "Waiting for the owner to confirm".
struct HomeProposedGoals: View {
    let viewModel: HomeFollowThroughViewModel
    @State private var note: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Waiting for you", title: "Proposed goals",
                              trailing: viewModel.proposedGoals.count > 1 ? "\(viewModel.proposedGoals.count)" : nil)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(viewModel.proposedGoals.enumerated()), id: \.element.id) { index, goal in
                    VStack(alignment: .leading, spacing: 6) {
                        HomeMixedText.make(goal.summary, size: CavnarType.body, weight: 700, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        HomeMixedText.make(goal.byLine + " \u{00B7} once confirmed, every module judges against it",
                                           size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                        if viewModel.canConfirmGoals {
                            HStack(spacing: 18) {
                                ForEach([true, false], id: \.self) { confirm in
                                    Button {
                                        Haptic.light()
                                        Task {
                                            if let said = await viewModel.answerGoal(goal, confirm: confirm) {
                                                withAnimation { note = said }
                                            }
                                        }
                                    } label: {
                                        Text(confirm ? "Confirm" : "Decline")
                                            .font(.cavnarBody(CavnarType.secondary, weight: confirm ? 700 : 600))
                                            .foregroundStyle(confirm ? Color.cavnarEmber2 : Color.cavnarInk3)
                                            .frame(minHeight: 36)
                                            .contentShape(Rectangle())
                                    }
                                    .buttonStyle(.plain)
                                    .disabled(viewModel.answeringGoal != nil)
                                }
                                Spacer(minLength: 0)
                            }
                            if viewModel.answeringGoal == goal.id {
                                CavnarSkeletonBar(height: 3)
                            }
                        } else {
                            Text("Waiting for the owner to confirm")
                                .font(.cavnarBody(CavnarType.caption, weight: 600))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .padding(.vertical, 10)
                    if index < viewModel.proposedGoals.count - 1 { AccountRowDivider() }
                }
            }
            .cavnarCard()
            if let note {
                Text(note)
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                    .transition(.opacity)
            }
        }
    }
}

/// "Renew or close" — a goal whose date passed without reaching it, retired
/// from every prompt after two weeks and asked once (memory re-audit R3):
/// Another month (the same target, a new date) or Close it — the web Goals
/// card's two buttons (parity audit #66). A teammate sees it read-only.
struct HomeMissedGoals: View {
    let viewModel: HomeFollowThroughViewModel
    @State private var note: String?
    @State private var closing: ProposedGoal?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Renew or close", title: "Missed goals",
                              trailing: viewModel.missedGoals.count > 1 ? "\(viewModel.missedGoals.count)" : nil)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(viewModel.missedGoals.enumerated()), id: \.element.id) { index, goal in
                    VStack(alignment: .leading, spacing: 6) {
                        HomeMixedText.make(goal.summary, size: CavnarType.body, weight: 700, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("Its date passed without reaching it")
                            .font(.cavnarBody(CavnarType.caption, weight: 500))
                            .foregroundStyle(Color.cavnarInk3)
                        if viewModel.canConfirmGoals {
                            HStack(spacing: 18) {
                                Button {
                                    Haptic.light()
                                    Task {
                                        if let said = await viewModel.answerMissed(goal, renew: true) {
                                            withAnimation { note = said }
                                        }
                                    }
                                } label: {
                                    Text("Another month")
                                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                        .foregroundStyle(Color.cavnarEmber2)
                                        .frame(minHeight: 44)
                                        .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                Button {
                                    Haptic.light()
                                    closing = goal
                                } label: {
                                    Text("Close it")
                                        .font(.cavnarBody(CavnarType.secondary, weight: 600))
                                        .foregroundStyle(Color.cavnarInk3)
                                        .frame(minHeight: 44)
                                        .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                Spacer(minLength: 0)
                            }
                            .disabled(viewModel.answeringGoal != nil)
                            if viewModel.answeringGoal == goal.id {
                                CavnarSkeletonBar(height: 3)
                            }
                        } else {
                            Text("Waiting for the owner to renew or close it")
                                .font(.cavnarBody(CavnarType.caption, weight: 600))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .padding(.vertical, 10)
                    if index < viewModel.missedGoals.count - 1 { AccountRowDivider() }
                }
            }
            .cavnarCard()
            if let note {
                Text(note)
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                    .transition(.opacity)
            } else if let error = viewModel.missedGoalError {
                Text(error)
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarRed)
            }
        }
        .confirmationDialog("Close this goal?", isPresented: Binding(get: { closing != nil },
                                                                     set: { if !$0 { closing = nil } }),
                            titleVisibility: .visible, presenting: closing) { goal in
            Button("Close it", role: .destructive) {
                Task {
                    if let said = await viewModel.answerMissed(goal, renew: false) {
                        withAnimation { note = said }
                    }
                }
            }
            Button("Keep it", role: .cancel) {}
        } message: { goal in
            Text("\u{201C}\(goal.summary)\u{201D} stops being a target. You can set a new goal any time.")
        }
    }
}
