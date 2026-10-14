import SwiftUI
import Observation

struct StaffContact: Decodable, Identifiable, Hashable {
    let employeeName: String
    let email: String
    let phone: String
    /// How Send reaches them — "app", "text" or "email" (people.reach);
    /// nil when nothing does. Absent from an older server, which then
    /// counts an email address alone, as before.
    var channel: String? = nil
    /// True when the server sent `channel` at all (null included).
    var channelKnown = false

    var id: String { employeeName }
    /// Reached by the app, a text they asked for, or email — not only an
    /// email address (the sheet counted email alone and said "Add an email
    /// address first" to a week whose staff all had the app).
    var isReachable: Bool { channelKnown ? channel != nil : !email.isEmpty }

    /// The line under the name: how this person gets the week.
    var reachLine: String {
        switch channel {
        case "app": return "In the app"
        case "text": return "By text"
        case "email": return email.isEmpty ? "By email" : email
        default:
            if !channelKnown, !email.isEmpty { return email }
            return "No app, texts or email \u{2014} tap to add an email"
        }
    }

    enum CodingKeys: String, CodingKey {
        case employeeName = "employee_name"
        case email, phone, channel
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        employeeName = try c.decode(String.self, forKey: .employeeName)
        email = (try? c.decodeIfPresent(String.self, forKey: .email)) ?? ""
        phone = (try? c.decodeIfPresent(String.self, forKey: .phone)) ?? ""
        channelKnown = c.contains(.channel)
        channel = try? c.decodeIfPresent(String.self, forKey: .channel)
    }
}

/// `people.reach_summary`: how many of the week's people each channel
/// reaches — the app first, then a text they opted into, then email.
struct PublishReach: Decodable, Equatable {
    var total = 0
    var reachable = 0
    var byApp = 0
    var byText = 0
    var byEmail = 0
    var unreachable: [String] = []

    enum CodingKeys: String, CodingKey {
        case total, reachable, unreachable
        case byApp = "by_app"
        case byText = "by_text"
        case byEmail = "by_email"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        total = (try? c.decodeIfPresent(Int.self, forKey: .total)) ?? 0
        reachable = (try? c.decodeIfPresent(Int.self, forKey: .reachable)) ?? 0
        byApp = (try? c.decodeIfPresent(Int.self, forKey: .byApp)) ?? 0
        byText = (try? c.decodeIfPresent(Int.self, forKey: .byText)) ?? 0
        byEmail = (try? c.decodeIfPresent(Int.self, forKey: .byEmail)) ?? 0
        unreachable = (try? c.decodeIfPresent([String].self, forKey: .unreachable)) ?? []
    }

    /// "Reaches 12 of 14 · 9 in the app, 1 by text, 2 by email · 2 only in
    /// the staff portal" — the web's psReach line.
    var line: String {
        var bits: [String] = []
        if byApp > 0 { bits.append("\(byApp) in the app") }
        if byText > 0 { bits.append("\(byText) by text") }
        if byEmail > 0 { bits.append("\(byEmail) by email") }
        var out = "Reaches \(reachable) of \(total)"
        if !bits.isEmpty { out += " · " + bits.joined(separator: ", ") }
        if !unreachable.isEmpty { out += " · \(unreachable.count) only in the staff portal" }
        return out
    }
}

/// `GET /mobile/api/labor/publish-check` — what pressing Send would do,
/// before it is pressed: the week, the gate's blockers (with the keys an
/// acknowledgement names) and who each channel reaches. With no
/// `schedule_id` it answers for the OPEN draft, or `schedule_id: null`.
struct PublishCheck: Decodable, Equatable {
    /// One blocker: its key, its words, its TIER — must-fix (red) or a
    /// decision (amber) — and, for a must-fix, how it may be sent past: the
    /// law an edit can fix never, the posting law with the owner's
    /// acknowledgement, a manager gap by a manager or owner on the roster
    /// (schedule re-audit 10/11/26 TRUST-24, TRUST-4/5/8). The gate's
    /// `demotable` (formerly `hard`) says only whether the owner's record
    /// may quiet it, and is not a colour.
    struct Item: Decodable, Equatable {
        let key: String
        let text: String
        var tier: String? = nil
        var law = false
        var fixable = true
        var cls: String? = nil
        var kind: String? = nil
        var coverCandidates: [String] = []
        var legalText: String? = nil
        var overriddenBefore: String? = nil

        enum CodingKeys: String, CodingKey {
            case key, text, tier, law, fixable, cls, kind, legal
            case coverCandidates = "cover_candidates"
            case overriddenBefore = "overridden_before"
        }
        private enum LegalKeys: String, CodingKey { case text }

        init(key: String, text: String, tier: String? = nil) {
            self.key = key; self.text = text; self.tier = tier
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = try c.decode(String.self, forKey: .key)
            text = try c.decode(String.self, forKey: .text)
            tier = c.sfText(.tier)
            law = c.sfBool(.law) ?? false
            fixable = c.sfBool(.fixable) ?? true
            cls = c.sfText(.cls)
            kind = c.sfText(.kind)
            coverCandidates = c.setupTexts(.coverCandidates)
            legalText = (try? c.nestedContainer(keyedBy: LegalKeys.self, forKey: .legal))?.sfText(.text)
            overriddenBefore = PublishReview.MustFix.overriddenLine(
                (try? c.decodeIfPresent(LooseValue.self, forKey: .overriddenBefore)) ?? nil)
        }

        /// In the must-fix tier (or from before the tiers, which the gate
        /// reads as must-fix).
        var isMustFix: Bool { PublishReview.MustFix.isMustFixTier(tier) }

        /// As a must-fix, overridable the way the gate's override_policy
        /// says — the web's fromCheck, item for item.
        var mustFix: PublishReview.MustFix {
            var m = PublishReview.MustFix(key: key, text: text, tier: tier ?? "must_fix", hard: true,
                                          overriddenBefore: overriddenBefore)
            m.law = law
            m.cls = cls
            m.kind = kind
            m.candidates = coverCandidates
            m.legalText = legalText
            if law {
                m.overrideAllowed = !fixable
                m.overrideNeeds = fixable ? nil : "legal_ack"
                m.needsCoveredBy = false
                m.overrideWhy = fixable ? "This breaks the law \u{2014} fix it, or the week can\u{2019}t go to staff." : nil
                m.overridePrompt = fixable ? nil : legalText
            }
            return m
        }

        /// A decision-tier blocker as a decision that holds Send, answered
        /// by sending it as it is (the web's fromCheck).
        var decision: PublishReview.Decision {
            var d = PublishReview.Decision(key: key, text: text, options: [
                PublishReview.Action(label: "Send it as it is", action: "acknowledge",
                                     payload: ["key": .string(key)], scope: "week")], holds: true)
            d.kind = kind
            d.overriddenBefore = overriddenBefore
            return d
        }
    }
    var ok = false
    var scheduleId: Int? = nil
    var weekStart: String? = nil
    var weekEnd: String? = nil
    var publishedAt: String? = nil
    var blockers: [String] = []
    var blockerKeys: [String] = []
    var blockerItems: [Item] = []
    var reach: PublishReach? = nil
    var unsentChanges: [String]? = nil
    var canPublish = false
    /// What is worth a look but never holds the send (SQ-29), the week's
    /// hours split by pay (E-7) and the rows the manager's own record says
    /// they are likely to change (L-15) — G-1, H1-6.
    var notes: [PublishNote] = []
    var hours: PublishHours? = nil
    var likelyToChange: LikelyToChange? = nil
    /// Why this copy cannot be sent: a newer copy of the week replaced it
    /// (schedule re-audit 10/4/26 UI-3). Nil when it can.
    var replacedReason: String? = nil
    /// The server's own verdict on sending this week in one tap
    /// (strategy_routes.draft_one_tap_safe): an unattended check — no
    /// blocker, no soft flag automation is held by, no unread note, this
    /// login may send, somebody it reaches. Nil from an older server: never
    /// one tap (re-audit 10/8/26 #1).
    var oneTapSafe: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case ok, blockers, reach, notes, hours
        case oneTapSafe = "one_tap_safe"
        case replacedReason = "replaced_reason"
        case likelyToChange = "likely_to_change"
        case scheduleId = "schedule_id"
        case weekStart = "week_start"
        case weekEnd = "week_end"
        case publishedAt = "published_at"
        case blockerKeys = "blocker_keys"
        case blockerItems = "blocker_items"
        case unsentChanges = "unsent_changes"
        case canPublish = "can_publish"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? false
        scheduleId = try? c.decodeIfPresent(Int.self, forKey: .scheduleId)
        weekStart = try? c.decodeIfPresent(String.self, forKey: .weekStart)
        weekEnd = try? c.decodeIfPresent(String.self, forKey: .weekEnd)
        publishedAt = try? c.decodeIfPresent(String.self, forKey: .publishedAt)
        blockers = (try? c.decodeIfPresent([String].self, forKey: .blockers)) ?? []
        blockerKeys = (try? c.decodeIfPresent([String].self, forKey: .blockerKeys)) ?? []
        blockerItems = (try? c.decodeIfPresent([Item].self, forKey: .blockerItems)) ?? []
        reach = try? c.decodeIfPresent(PublishReach.self, forKey: .reach)
        unsentChanges = try? c.decodeIfPresent([String].self, forKey: .unsentChanges)
        canPublish = (try? c.decodeIfPresent(Bool.self, forKey: .canPublish)) ?? false
        notes = c.sfList(PublishNote.self, .notes)
        hours = (try? c.decodeIfPresent(PublishHours.self, forKey: .hours)) ?? nil
        likelyToChange = (try? c.decodeIfPresent(LikelyToChange.self, forKey: .likelyToChange)) ?? nil
        replacedReason = (try? c.decodeIfPresent(String.self, forKey: .replacedReason)) ?? nil
        oneTapSafe = (try? c.decodeIfPresent(Bool.self, forKey: .oneTapSafe)) ?? nil
    }

    /// Whether this week may go out in one tap — Waiting on you's Send and
    /// the drafted push's Send to staff. The server's verdict only: the
    /// phone never re-derives it from the blockers alone, which missed the
    /// soft flags and unread notes (re-audit 10/8/26 #1). The publish route
    /// runs the same gate again on `one_tap: true`.
    func allowsOneTap(scheduleId id: Int) -> Bool {
        ok && scheduleId == id && oneTapSafe == true && canPublish && publishedAt == nil && replacedReason == nil
    }

    /// The lines to show and the keys they carry, items first.
    var shown: (lines: [String], keys: [String]) {
        if !blockerItems.isEmpty { return (blockerItems.map(\.text), blockerItems.map(\.key)) }
        return (blockers, blockerKeys)
    }
}

/// Who was sent the schedule, and who has actually opened it — the
/// difference between "I sent it" and "the closing server has seen it".
struct ScheduleShareStatus: Decodable, Identifiable, Hashable {
    let employeeName: String
    let sentTo: String?
    let sentAt: String?
    let viewedAt: String?
    let viewCount: Int

    var id: String { employeeName }
    var hasViewed: Bool { viewedAt != nil }

    enum CodingKeys: String, CodingKey {
        case employeeName = "employee_name"
        case sentTo = "sent_to"
        case sentAt = "sent_at"
        case viewedAt = "viewed_at"
        case viewCount = "view_count"
    }
}

struct PublishResult: Decodable {
    let ok: Bool
    let sent: [Sent]
    let unreachable: [Unreachable]
    let failed: [Failed]
    let error: String?
    // True when the owner sent past the publish gate's blockers — said
    // in the result so "sent" never quietly hides that it was.
    let acknowledged: Bool?
    /// This week had already gone to staff; nothing was sent twice.
    let alreadyPublished: Bool?
    /// Published to the staff portal with no emails (nobody has an address).
    let note: String?
    /// A week staff already had: only the people whose shifts changed were
    /// told (the save no longer emails anyone; Send does).
    let changesSent: Bool?
    /// The version the Send wrote: the Labor screen keeps it with its rows,
    /// so the owner's next save names it (schedule re-audit 10/4/26 UI-4).
    var version: Int? = nil
    /// The restaurant's send window (send_delay_minutes > 0): nothing went
    /// out yet — the send is a delayed action that leaves in `undoMinutes`
    /// and `actionId` stops it (/mobile/api/actions/<id>/cancel). `sent` is
    /// empty because nobody has been told (re-audit 10/8/26 H1).
    var queued = false
    var actionId: Int? = nil
    var executeAt: String? = nil
    var undoMinutes: Int? = nil
    /// The people the Send's inline window did not reach yet — the outbox
    /// job tells them within the minute (schedule re-audit 10/11/26
    /// BUGSLIFE-21: the phone said "sent to N" and nothing of the rest).
    var sending: [String] = []
    /// Nobody was told: the week is only in the staff portal.
    var portalOnly = false
    /// The outbox as it stands (still sending, failed, unreachable) —
    /// polled while anyone is still being sent to.
    var delivery: PublishDeliveryCounts? = nil
    /// Covers and swaps this send undid, each told (BUGSLIFE-1).
    var staffChangesUndone: [String] = []

    enum CodingKeys: String, CodingKey {
        case ok, sent, unreachable, failed, error, acknowledged, note, version, queued, sending, delivery
        case alreadyPublished = "already_published"
        case changesSent = "changes_sent"
        case actionId = "action_id"
        case executeAt = "execute_at"
        case undoMinutes = "undo_minutes"
        case portalOnly = "portal_only"
        case staffChangesUndone = "staff_changes_undone"
    }

    // Lenient: a changes-only send may answer without every list.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        sent = (try? c.decodeIfPresent([Sent].self, forKey: .sent)) ?? []
        unreachable = (try? c.decodeIfPresent([Unreachable].self, forKey: .unreachable)) ?? []
        failed = (try? c.decodeIfPresent([Failed].self, forKey: .failed)) ?? []
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        acknowledged = try? c.decodeIfPresent(Bool.self, forKey: .acknowledged)
        alreadyPublished = try? c.decodeIfPresent(Bool.self, forKey: .alreadyPublished)
        note = try? c.decodeIfPresent(String.self, forKey: .note)
        changesSent = try? c.decodeIfPresent(Bool.self, forKey: .changesSent)
        version = (try? c.decodeIfPresent(Int.self, forKey: .version)) ?? nil
        queued = ((try? c.decodeIfPresent(Bool.self, forKey: .queued)) ?? nil) ?? false
        actionId = (try? c.decodeIfPresent(Int.self, forKey: .actionId)) ?? nil
        executeAt = (try? c.decodeIfPresent(String.self, forKey: .executeAt)) ?? nil
        undoMinutes = (try? c.decodeIfPresent(Int.self, forKey: .undoMinutes)) ?? nil
        // `sending` is the names still to go; an older server's count reads
        // as that many unnamed people.
        let names = c.setupTexts(.sending)
        if !names.isEmpty {
            sending = names
        } else if let n = c.sfInt(.sending), n > 0 {
            sending = Array(repeating: "", count: n)
        }
        portalOnly = c.sfBool(.portalOnly) ?? false
        delivery = (try? c.decodeIfPresent(PublishDeliveryCounts.self, forKey: .delivery)) ?? nil
        staffChangesUndone = c.setupTexts(.staffChangesUndone)
    }

    /// "4 still sending" — the people not yet reached, from the outbox when
    /// it has been read since, else the Send's own list.
    func stillSending(_ now: PublishDeliveryCounts?) -> Int {
        (now ?? delivery)?.sending ?? sending.count
    }

    struct Sent: Decodable, Identifiable {
        let employeeName: String
        let sentTo: String
        let shifts: Int
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case sentTo = "sent_to"
            case shifts
        }
    }

    struct Unreachable: Decodable, Identifiable {
        let employeeName: String
        let reason: String
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case reason
        }
    }

    struct Failed: Decodable, Identifiable {
        let employeeName: String
        let error: String
        var id: String { employeeName }
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case error
        }
    }
}

@Observable
@MainActor
final class PublishScheduleViewModel {
    var contacts: [StaffContact] = []
    var status: [ScheduleShareStatus] = []
    var weekLabel: String?
    var isLoading = false
    var errorMessage: String?

    var isPublishing = false
    var lastResult: PublishResult?
    // Which schedule_history row to send. Nil sends the latest, which is
    // what the Labor tab means; History passes the row it is looking at.
    var scheduleId: Int?
    /// The pre-publish review (schedule audit 10/10/26 TRUST-27, RM-6): the
    /// one payload the web's "Before it goes out" reads too — verdict, must
    /// fix, decide, the numbers with their track record, what changed,
    /// where the week is fragile, why these people, how sure. Nil until it
    /// loads, or from a server without it: the sheet then reads the publish
    /// check's blockers alone, hard ones still red.
    var review: PublishReview?
    /// The publish check's blockers, read only when the review could not
    /// run (TRUST-25): its must-fix tier as must-fix items and its decision
    /// tier as decisions that hold Send (TRUST-24) — and the 409's, when the
    /// gate answered the press with more.
    var checkItems: [PublishReview.MustFix] = []
    var checkDecisions: [PublishReview.Decision] = []
    var gateItems: [PublishReview.MustFix] = []
    var gateDecisions: [PublishReview.Decision] = []
    /// Each must-fix the owner sends past, by key, with their words — sent
    /// as `overrides: [{key, reason}]` and stored by key (TRUST-28). Each on
    /// its own (TRUST-2, TRUST-3); a group's one tap is a FIX, never an
    /// override for all of them.
    var overrides: [String: PublishOverrideChoice] = [:]
    /// The `holds` decisions answered here (their keys go back with Send as
    /// acknowledgements) — each needs an answer before the week goes out.
    var acknowledged: Set<String> = []
    /// A review tap that opens another screen (`open`, TRUST-20): the sheet
    /// closes and the screen that opened it goes there (`onOpen`).
    var dismissRequested = false
    var openTarget: ReviewOpenTarget?
    /// Where an `open` goes; nil from a screen that can't take it there
    /// (History), which then says so under the button.
    var onOpen: ((ReviewOpenTarget) -> Void)?
    /// A setting set on the web, opened over the sheet.
    var webPath: String?
    /// A person's record in Team, opened over the sheet ("Fix it in Team").
    var personTarget: PersonSheetTarget?
    /// The restaurant's targets, opened over the sheet (TRUST-29).
    var showingTargets = false
    /// The press the gate refused an override on, by key, with why (the
    /// 409's `override_refused`; TRUST-4/5).
    var refusedOverrides: [String: String] = [:]
    /// The rows on screen when they hold an edit no save has stored: the
    /// review is asked about them (`POST labor/publish-review`), not the
    /// stored copy.
    /// The review action running now (its id) and what each said back.
    var actionBusy: String?
    var actionNotes: [String: String] = [:]
    /// The decisions answered from this sheet, by key, with the answer.
    var answered: [String: String] = [:]
    /// Edits the week on screen (Labor's view model) — a fix that moves a
    /// shift. Nil from History: those fixes say to open the week.
    /// The Labor screen holds an edit no save has stored: Send would send
    /// the stored copy, so the sheet says so above the review.
    var hasUnsavedEdits = false
    var rowActionRunner: ((PublishReview.Action) async -> String?)?
    /// The version of the rows on screen after such a fix saved them — Send
    /// names it, so the week sent is the week just fixed (BUGSLIFE-11).
    var versionProvider: (() -> Int?)?
    /// The week against the usual crew (TRUST-12): the shifts it runs
    /// leaner, each with its answer, on the sheet the decision is made in.
    let crew = CrewMatrixStore()

    /// The crew's shifts on the sheet unless the review already asks about
    /// them as decisions (its `decide` keys name the crew).
    var showsCrew: Bool {
        guard publishedAt == nil else { return false }
        return !(review?.decide.contains { $0.key.lowercased().hasPrefix("crew") } ?? false)
    }

    /// When the week went to staff — the review's, else the check's.
    var publishedAt: String? { review?.publishedAt ?? check?.publishedAt }

    /// Puts the crew's suggested shift on the week through the row runner.
    var crewAddRow: (([String: LooseValue]) async -> String?)? {
        guard let runner = rowActionRunner else { return nil }
        return { row in await runner(PublishReview.Action(label: "Add 1 this week", action: "add_shift", payload: row)) }
    }
    /// The 409's "worth a look" notes, when the gate answered the press.
    var gateNotes: [PublishNote] = []
    /// People whose shifts changed since the week went out and who haven't
    /// been told (the save's `unsent_changes`). Send tells only them.
    var unsentChanges: [String] = []
    // A refusal that is not a gate: the login cannot send (403), or the
    // server said no. Separate from errorMessage, which hides the whole
    // sheet behind an error when nothing has loaded.
    var publishError: String?

    /// Told once a send went through (Labor clears its unsent-changes list).
    var onSent: (() -> Void)?
    /// The Send's version, for the screen holding the week's rows (UI-4).
    var onVersion: ((Int) -> Void)?
    /// Why this copy cannot be sent (publish-check's replaced_reason, UI-3).
    var replacedReason: String?

    var editingContact: StaffContact?
    var isSavingContact = false
    var contactError: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct ContactsResponse: Decodable {
        let ok: Bool
        let contacts: [StaffContact]
        let reachable: Int
        let weekStart: String?
        let weekEnd: String?
        let reach: PublishReach?

        enum CodingKeys: String, CodingKey {
            case ok, contacts, reachable, reach
            case weekStart = "week_start"
            case weekEnd = "week_end"
        }
    }

    /// The server's own pre-send read (publish-check): the blockers on
    /// screen BEFORE Send — they used to appear only after a 409. Nil until
    /// loaded, or from an older server.
    var check: PublishCheck?
    /// How the week reaches its people, by channel — from the contacts read
    /// of the week being sent.
    var reach: PublishReach?
    /// False when this login can draft but not send (publish-check's
    /// can_publish); true until known.
    var canPublish = true

    private struct StatusResponse: Decodable {
        let ok: Bool
        let status: [ScheduleShareStatus]
        /// Who was told and how it went, from the publish outbox.
        var delivery: PublishDelivery? = nil
    }
    /// The publish outbox's counts for the week on screen (schedule audit
    /// 10/10/26 BUGSLIFE-9): still sending, failed (retried), unreachable.
    typealias PublishDelivery = PublishDeliveryCounts
    var delivery: PublishDelivery?
    /// Polls the outbox while a send is still reaching people
    /// (BUGSLIFE-21); stopped when nobody is left or the sheet goes.
    private var deliveryPoll: Task<Void, Never>?
    /// The version of the rows the screen holds (Labor's own), sent with
    /// Send so a week changed since is refused, not sent unseen.
    var version: Int?
    private typealias OKErrorResponse = APIClient.OKResponse

    var reachableCount: Int { contacts.filter(\.isReachable).count }

    /// What must be fixed or overridden before Send: the review's list (or
    /// the publish check's), with anything the gate's 409 added since.
    var mustFix: [PublishReview.MustFix] {
        guard publishedAt == nil || !unsentChanges.isEmpty else { return gateItems }
        var out = review?.isUsable == true ? (review?.mustFix ?? []) : checkItems
        for item in gateItems where !out.contains(where: { $0.key == item.key }) { out.append(item) }
        return out
    }

    /// The items still holding Send.
    var openItems: [PublishReview.MustFix] { PublishGate.open(mustFix, overrides: overrides) }

    /// Every decision on the sheet: the review's (or, without it, the
    /// check's decision tier), with any the gate's 409 added since.
    var decisions: [PublishReview.Decision] {
        var out = review?.isUsable == true ? (review?.decide ?? []) : checkDecisions
        for d in gateDecisions where !out.contains(where: { $0.key == d.key }) { out.append(d) }
        return out
    }

    /// The decisions that hold Send, answered or not.
    var holdingDecisions: [PublishReview.Decision] { decisions.filter(\.holds) }

    /// The decisions Send waits on that have no answer yet.
    var undecided: [PublishReview.Decision] {
        guard publishedAt == nil else { return [] }
        return holdingDecisions.filter { !acknowledged.contains($0.key) }
    }

    /// What Send acknowledges: each override's key and each answered
    /// decision's, still on the lists.
    var acknowledgeKeys: [String] {
        let fromOverrides = PublishGate.overrides(mustFix, overrides).map(\.key)
        let decided = holdingDecisions.map(\.key).filter { acknowledged.contains($0) }
        return fromOverrides + decided.filter { !fromOverrides.contains($0) }
    }

    func load() async {
        isLoading = contacts.isEmpty
        errorMessage = nil
        defer { isLoading = false }
        do {
            // The people on the week being SENT — the route used to answer
            // with the newest week's, whichever week this sheet was sending.
            let query = scheduleId.map { ["schedule_id": String($0)] } ?? [:]
            let r: ContactsResponse = try await client.send("/mobile/api/labor/staff-contacts", query: query)
            contacts = r.contacts
            reach = r.reach
            if let start = r.weekStart {
                // M/D/YY, not the ISO dates the route sends (CLIENT-45).
                weekLabel = r.weekEnd.map { CavnarDate.mdyRange(start, $0) } ?? CavnarDate.mdy(start)
            }
            // Who opened THIS week — without the id the route answers for
            // the newest week, whichever one the sheet is sending (M2).
            let s: StatusResponse? = try? await client.send(
                "/mobile/api/labor/schedule-share-status", query: query, hapticOnError: false)
            status = s?.status ?? []
            delivery = s?.delivery
            // ONE gate run per load (schedule re-audit 10/11/26 TRUST-25):
            // the review carries the send, reach, sent state and the gate's
            // own items; the publish check is read only when it fails.
            await refreshGate()
            if publishedAt == nil, let id = scheduleId {
                crew.onAnswered = { [weak self] in
                    if let provider = self?.versionProvider { self?.version = provider() }
                    await self?.refreshGate()
                }
                await crew.load(historyId: id)
            }
        } catch let error as APIClient.APIError {
            if contacts.isEmpty { errorMessage = error.message }
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch {
            if contacts.isEmpty { errorMessage = "Couldn't load your staff list." }
        }
    }

    /// Blockers before Send, as the web's psRefresh reads them. A week staff
    /// already have names only who a saved change moved; the gate's
    /// warnings are for a week staff don't have yet.
    /// The gate, read once: the review, else (it failed, or an older server
    /// has none) the publish check — never both (TRUST-25).
    func refreshGate() async {
        if await loadReview() { return }
        await loadCheck()
    }

    func loadCheck() async {
        guard let scheduleId else { return }
        guard let c: PublishCheck = try? await client.send(
            "/mobile/api/labor/publish-check", query: ["schedule_id": String(scheduleId)],
            hapticOnError: false), c.ok else { return }
        check = c
        canPublish = c.canPublish
        replacedReason = c.replacedReason
        if c.publishedAt == nil {
            // The tier, not the old `hard` flag (TRUST-24): a decision
            // (notice, a late change, an unscored week) is amber and answered
            // in one tap, never a red must-fix that wants a reason.
            if c.blockerItems.isEmpty {
                checkItems = zip(c.blockerKeys, c.blockers).map { PublishReview.MustFix(key: $0.0, text: $0.1) }
                checkDecisions = []
            } else {
                checkItems = c.blockerItems.filter(\.isMustFix).map(\.mustFix)
                checkDecisions = c.blockerItems.filter { !$0.isMustFix }.map(\.decision)
            }
        } else if unsentChanges.isEmpty, let un = c.unsentChanges {
            unsentChanges = un
        }
        pruneOverrides()
    }

    /// The pre-publish review of this week; true when it came back usable.
    /// An older server answers 404 and the sheet reads the publish check.
    @discardableResult
    func loadReview() async -> Bool {
        guard let scheduleId else { return false }
        do {
            // The STORED week: Send sends what is stored, so the review is of
            // that copy — a screen with an unsaved edit is said instead
            // (`hasUnsavedEdits`), never reviewed as if it were the week.
            let r: PublishReview = try await client.send("/mobile/api/labor/publish-review",
                                                         query: ["schedule_id": String(scheduleId)],
                                                         hapticOnError: false)
            review = r.isUsable ? r : nil
        } catch is CancellationError {
            return review != nil
        } catch {
            review = nil
        }
        if let r = review {
            // What the publish check used to answer, from the same payload.
            check = nil
            canPublish = r.canPublish ?? true
            replacedReason = r.replacedReason
            if r.publishedAt != nil, unsentChanges.isEmpty, let un = r.send.unsentChanges { unsentChanges = un }
            if r.publishedAt != nil, let d = r.send.delivery { delivery = d }
        }
        pruneOverrides()
        return review != nil
    }

    /// While a send is still reaching people, read the outbox again every
    /// few seconds (at most two minutes) so "4 still sending" counts down
    /// and a failure shows — the web's notice_status (BUGSLIFE-21).
    func watchDelivery() {
        deliveryPoll?.cancel()
        guard let scheduleId else { return }
        let query = ["schedule_id": String(scheduleId)]
        deliveryPoll = Task { [weak self] in
            for _ in 0..<24 {
                try? await Task.sleep(for: .seconds(5))
                if Task.isCancelled { return }
                guard let self else { return }
                let s: StatusResponse? = try? await self.client.send(
                    "/mobile/api/labor/schedule-share-status", query: query, hapticOnError: false)
                if let s {
                    self.status = s.status
                    self.delivery = s.delivery ?? self.delivery
                }
                if (self.delivery?.sending ?? 0) == 0 { return }
            }
        }
    }

    func stopWatchingDelivery() {
        deliveryPoll?.cancel()
        deliveryPoll = nil
    }

    /// An override is for an item on the list: one since fixed is dropped,
    /// so it is never sent (or shown) for something no longer there.
    private func pruneOverrides() {
        let keys = Set(mustFix.map(\.key))
        overrides = overrides.filter { keys.contains($0.key) }
        let holding = Set(holdingDecisions.map(\.key))
        if review != nil || check != nil { acknowledged = acknowledged.filter { holding.contains($0) } }
    }

    /// Run one of the review's taps: a fix that edits the week (Labor's own
    /// edit, re-score and save), a decision's answer, a fragile spot's
    /// remedy. Then the review and the check are read again, so the list
    /// shows what is left.
    func run(_ action: PublishReview.Action, decision: String? = nil) async {
        guard actionBusy == nil else { return }
        // Answered on the sheet, sent with Send: an acknowledgement of a
        // decision that holds the week (the budget, the notice rule).
        if action.action == "acknowledge" {
            let key = LaborViewModel.payloadText(action.payload, "key") ?? decision ?? ""
            if !key.isEmpty { acknowledged.insert(key) }
            if let decision { answered[decision] = action.buttonLabel }
            Haptic.selection()
            return
        }
        // One answer for a group of one rule's decisions (TRUST-15): each
        // key acknowledged, sent with Send like a single answer.
        if action.action == "acknowledge_all" {
            var keys: [String] = []
            if case .list(let l)? = action.payload["keys"] {
                keys = l.compactMap { if case .string(let s) = $0 { return s } else { return nil } }
            }
            for k in keys {
                acknowledged.insert(k)
                answered[k] = action.buttonLabel
            }
            Haptic.selection()
            return
        }
        // Goes where its payload says (TRUST-20) — Redo, Setup's managers,
        // the person in Team, the shift on the week, a web setting — never
        // a sheet that only closes.
        if action.action == "open" {
            openAction(action)
            return
        }
        // "Leave them off this week", "Keep 3": kept on the server, so it is
        // not asked again while it holds (TRUST-17) — it used to live only
        // on this sheet and came back on every check.
        if action.action == "dismiss" {
            actionBusy = action.id
            defer { actionBusy = nil }
            if let refusal = await dismissSuggestion(action, decision: decision) {
                actionNotes[action.id] = refusal
                Haptic.error()
                return
            }
            actionNotes[action.id] = nil
            if let decision { answered[decision] = action.buttonLabel }
            Haptic.selection()
            return
        }
        actionBusy = action.id
        defer { actionBusy = nil }
        var refusal: String?
        if LaborViewModel.rowActions.contains(action.action) {
            if let runner = rowActionRunner {
                refusal = await runner(action)
            } else {
                refusal = "Open the week on the Labor screen to make this change."
            }
        } else {
            refusal = await post(action)
        }
        if let refusal {
            actionNotes[action.id] = refusal
            Haptic.error()
            return
        }
        actionNotes[action.id] = nil
        if let decision { answered[decision] = action.buttonLabel }
        if let provider = versionProvider { version = provider() }
        Haptic.success()
        await refreshGate()
    }

    /// An `open` tap: a web setting opens over the sheet; anything else
    /// closes the sheet and the screen that opened it goes there. A screen
    /// that can't (History) says where to do it instead.
    func openAction(_ action: PublishReview.Action) {
        guard let target = ReviewOpenTarget.from(action.payload) else {
            actionNotes[action.id] = "Open the week on the Labor screen to do this."
            return
        }
        Haptic.light()
        if case .web(let path) = target {
            webPath = path
            return
        }
        if case .targets = target {
            showingTargets = true
            return
        }
        if case .person(let who) = target {
            // The person's record in Team opens over this sheet, from
            // anywhere the sheet was opened — the review stays underneath.
            personTarget = PersonSheetTarget(key: nil, name: who)
            return
        }
        guard let onOpen else {
            actionNotes[action.id] = "Open this week on the Labor screen to do this."
            return
        }
        openTarget = target
        dismissRequested = true
        onOpen(target)
    }

    private struct DismissBody: Encodable {
        let key: String
        let scope: String
        var until: String? = nil
        var evidence: String? = nil
        var scheduleId: Int? = nil
        var answer: String? = nil
        enum CodingKeys: String, CodingKey {
            case key, scope, until, evidence, answer
            case scheduleId = "schedule_id"
        }
    }

    /// POST labor/publish-review/dismiss — the web's own call (TRUST-17).
    /// Nil when kept; else why not.
    private func dismissSuggestion(_ action: PublishReview.Action, decision: String?) async -> String? {
        let p = action.payload
        guard let key = LaborViewModel.payloadText(p, "key") ?? decision else { return nil }
        let body = DismissBody(key: key, scope: LaborViewModel.payloadText(p, "scope") ?? action.scope ?? "week",
                               until: LaborViewModel.payloadText(p, "until") ?? review?.weekEnd,
                               evidence: LaborViewModel.payloadText(p, "evidence"),
                               scheduleId: scheduleId, answer: action.buttonLabel)
        do {
            let r: OKErrorResponse = try await client.send("/mobile/api/labor/publish-review/dismiss", method: .post,
                                                           body: body, hapticOnError: false, retryTransient: false)
            return r.ok ? nil : (r.error ?? "Kept for now \u{2014} it may be asked again.")
        } catch let error as APIClient.APIError {
            return error.message
        } catch {
            return "Kept for now \u{2014} it may be asked again."
        }
    }

    private struct CrewAnswerBody: Encodable {
        let role: String
        let day: String
        let part: String
        let answer: String
        let historyId: Int?
        enum CodingKeys: String, CodingKey {
            case role, day, part, answer
            case historyId = "history_id"
        }
    }

    private struct CrewAnswerResponse: Decodable {
        let ok: Bool
        let error: String?
        var suggestion: [String: LooseValue]? = nil
    }

    /// The review's standby ask, as the route reads it.
    private struct StandbyAskBody: Encodable {
        let date: String
        let employee: String
        var shiftStart: String? = nil
        var shiftEnd: String? = nil
        enum CodingKeys: String, CodingKey {
            case date, employee
            case shiftStart = "shift_start"
            case shiftEnd = "shift_end"
        }
    }

    /// A floor lowered on one weekday and meal: the role's floors as the
    /// review wrote them (`role_floors`, merged role by role).
    private struct RoleFloorsBody: Encodable {
        let roleFloors: [String: LooseValue]
        enum CodingKeys: String, CodingKey { case roleFloors = "role_floors" }
    }

    private struct PostAnswer: Decodable {
        let ok: Bool
        let error: String?
        var suggestion: [String: LooseValue]? = nil
    }

    /// `post` {path, body}: the review's own write — a Crew answer or a
    /// standby ask — to the phone's twin of the path, then the review is
    /// read again. Only the two paths the review names.
    private func postAction(_ p: [String: LooseValue]) async -> String? {
        let path = LaborViewModel.payloadText(p, "path") ?? ""
        var body: [String: LooseValue] = [:]
        if case .object(let o)? = p["body"] { body = o }
        do {
            let r: PostAnswer
            switch path.trimmingCharacters(in: CharacterSet(charactersIn: "/")) {
            case "labor/crew-matrix":
                guard let role = LaborViewModel.payloadText(body, "role"), let day = LaborViewModel.payloadText(body, "day"),
                      let part = LaborViewModel.payloadText(body, "part"),
                      let answer = LaborViewModel.payloadText(body, "answer") else { return "That answer couldn\u{2019}t be read." }
                let hid = LaborViewModel.payloadText(body, "history_id").flatMap { Int($0) } ?? scheduleId
                r = try await client.send("/mobile/api/labor/crew-matrix", method: .post,
                                          body: CrewAnswerRequest(role: role, day: day, part: part, answer: answer,
                                                                  historyId: hid),
                                          hapticOnError: false, retryTransient: false)
                if r.ok, answer == "add" {
                    guard let row = r.suggestion else { return r.error ?? "Nobody can legally take it this week." }
                    guard let runner = rowActionRunner else { return "Open the week on the Labor screen to add it." }
                    return await runner(PublishReview.Action(label: "Add 1 this week", action: "add_shift", payload: row))
                }
            case "labor/standby/ask":
                guard let date = LaborViewModel.payloadText(body, "date"),
                      let who = LaborViewModel.payloadText(body, "employee") else { return "That ask couldn\u{2019}t be read." }
                r = try await client.send("/mobile/api/labor/standby/ask", method: .post,
                                          body: StandbyAskBody(date: date, employee: who,
                                                               shiftStart: LaborViewModel.payloadText(body, "shift_start"),
                                                               shiftEnd: LaborViewModel.payloadText(body, "shift_end")),
                                          hapticOnError: false, retryTransient: false)
            case "labor/rules":
                // A floor the work never needed, lowered on that weekday and
                // meal — the owner's own floors, role by role; or a rule the
                // owner always sends past, changed in one tap (TRUST-10:
                // `rules` merged key by key, never a law-backed setting).
                if case .object(let floors)? = body["role_floors"] {
                    r = try await client.send("/mobile/api/labor/rules", method: .post,
                                              body: RoleFloorsBody(roleFloors: floors),
                                              hapticOnError: false, retryTransient: false)
                } else if case .object(let rules)? = body["rules"], !rules.isEmpty {
                    r = try await client.send("/mobile/api/labor/rules", method: .post,
                                              body: ["rules": LooseValue.object(rules)],
                                              hapticOnError: false, retryTransient: false)
                } else {
                    return "That change couldn\u{2019}t be read."
                }
            default:
                return "Open the week on the web to do this one."
            }
            return r.ok ? nil : (r.error ?? "That couldn\u{2019}t be saved.")
        } catch let error as APIClient.APIError {
            return error.message
        } catch {
            return "That couldn\u{2019}t be saved."
        }
    }

    /// The taps that are not an edit of the rows: the Crew stage's answers
    /// (keep the usual crew as a minimum, leaner is fine, one more this
    /// week) — the same route the Crew screen and the web use.
    private func post(_ action: PublishReview.Action) async -> String? {
        let p = action.payload
        if action.action == "post" { return await postAction(p) }
        let crew: [String: String] = ["crew_hold": "hold", "crew_keep": "hold", "crew_lean": "lean",
                                      "crew_clear": "clear", "crew_add": "add"]
        var answer = crew[action.action]
        if action.action == "crew_answer" { answer = LaborViewModel.payloadText(p, "answer") }
        guard let answer, let role = LaborViewModel.payloadText(p, "role"),
              let day = LaborViewModel.payloadText(p, "day"), let part = LaborViewModel.payloadText(p, "part") else {
            return "Open the week on the web to do this one."
        }
        do {
            let r: CrewAnswerResponse = try await client.send(
                "/mobile/api/labor/crew-matrix", method: .post,
                body: CrewAnswerBody(role: role, day: day, part: part, answer: answer, historyId: scheduleId),
                hapticOnError: false, retryTransient: false)
            guard r.ok else { return r.error ?? "That couldn\u{2019}t be saved." }
            if answer == "add" {
                // One more this week: the suggested shift goes on the week as
                // an edit, saved like any other (nothing standing is set).
                guard let row = r.suggestion else { return r.error ?? "Nobody can legally take it this week." }
                guard let runner = rowActionRunner else {
                    return "Open the week on the Labor screen to add it."
                }
                return await runner(PublishReview.Action(label: action.label, action: "add_shift", payload: row))
            }
            return nil
        } catch let error as APIClient.APIError {
            return error.message
        } catch {
            return "That couldn\u{2019}t be saved."
        }
    }

    private struct ContactBody: Encodable {
        let employeeName: String
        let email: String
        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case email
        }
    }

    @discardableResult
    func saveContact(_ name: String, email: String) async -> Bool {
        isSavingContact = true
        contactError = nil
        defer { isSavingContact = false }
        do {
            let r: OKErrorResponse = try await client.send(
                "/mobile/api/labor/staff-contacts", method: .post,
                body: ContactBody(employeeName: name, email: email))
            if r.ok {
                await load()
                return true
            }
            contactError = r.error ?? "Couldn't save that."
            return false
        } catch let error as APIClient.APIError {
            contactError = error.message
            return false
        } catch {
            contactError = "Couldn't save that."
            return false
        }
    }

    /// Always names the week (the server refuses a publish without one) and,
    /// when the owner read the gate's blockers, acknowledges exactly the keys
    /// shown — `true` only for an older gate that sent no keys.
    ///
    /// `oneTap` is a send nobody reads a sheet for (Waiting on you's Send,
    /// the drafted push's Send to staff): `one_tap: true` makes the server
    /// run the unattended gate first and refuse (409 `one_tap_refused`)
    /// anything it would not send unread. It never acknowledges.
    struct PublishBody: Encodable {
        let scheduleId: Int
        let acknowledge: Bool
        var keys: [String] = []
        var oneTap = false
        /// The version of the rows the owner reviewed: a week somebody
        /// changed since is refused with their changes (409 `stale`), never
        /// sent unseen (schedule audit 10/10/26 BUGSLIFE-11).
        var version: Int? = nil
        /// "all" — the owner's "Send again to everyone"; "missing" — the
        /// people whose message failed or who were unreachable then. A sent
        /// week used to answer already_published and send nothing
        /// (BUGSLIFE-9).
        var resend: String? = nil
        /// Each must-fix sent past on purpose, with the owner's reason
        /// (`overrides: [{key, reason}]`, TRUST-28 / the pre-publish
        /// contract): the server acknowledges each key and stores its reason
        /// by key. Never sent by a one-tap send.
        var overrides: [PublishOverride] = []
        enum CodingKeys: String, CodingKey {
            case acknowledge, version, resend, overrides
            case scheduleId = "schedule_id"
            case oneTap = "one_tap"
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(scheduleId, forKey: .scheduleId)
            try c.encodeIfPresent(version, forKey: .version)
            try c.encodeIfPresent(resend, forKey: .resend)
            if oneTap {
                try c.encode(false, forKey: .acknowledge)
                try c.encode(true, forKey: .oneTap)
            } else if acknowledge && !keys.isEmpty {
                try c.encode(keys, forKey: .acknowledge)
            } else {
                try c.encode(acknowledge, forKey: .acknowledge)
            }
            if !oneTap && !overrides.isEmpty { try c.encode(overrides, forKey: .overrides) }
        }
    }

    /// 409 from the gate: `{needs_ack, blockers, blocker_keys,
    /// blocker_items: [{key, text}], schedule_id}`.
    struct GateResponse: Decodable {
        typealias Item = PublishCheck.Item
        let needsAck: Bool?
        let blockers: [String]?
        let blockerKeys: [String]?
        let blockerItems: [Item]?
        let scheduleId: Int?
        /// Worth a look, never holding the send (SQ-29, G-1).
        var notes: HomeLenientList<PublishNote>? = nil
        /// A one-tap send the unattended check refused (re-audit 10/8/26
        /// #1): open the week and read it.
        var oneTapRefused: Bool? = nil
        /// The tiered gate's must-fix keys (each needs an override with a
        /// reason; a manager gap, who runs the floor): the rest are decisions.
        var mustFixKeys: [String]? = nil
        /// The must-fix keys no override sends past (a law an edit can fix)
        /// and each override the gate refused, with why (TRUST-4/5/8).
        var notOverridable: [String]? = nil
        var overrideRefused: [Refused]? = nil

        struct Refused: Decodable, Equatable {
            let key: String
            let why: String
        }

        enum CodingKeys: String, CodingKey {
            case blockers, notes
            case mustFixKeys = "must_fix_keys"
            case oneTapRefused = "one_tap_refused"
            case needsAck = "needs_ack"
            case blockerKeys = "blocker_keys"
            case blockerItems = "blocker_items"
            case scheduleId = "schedule_id"
            case notOverridable = "not_overridable"
            case overrideRefused = "override_refused"
        }

        /// The lines to show and the keys they carry, items first.
        var shown: (lines: [String], keys: [String]) {
            if let items = blockerItems, !items.isEmpty { return (items.map(\.text), items.map(\.key)) }
            return (blockers ?? [], blockerKeys ?? [])
        }

        /// Whether one blocker is in the must-fix tier: its own `tier`, else
        /// the tiered gate's key list, else (a gate from before the tiers)
        /// must-fix.
        private func isMustFix(_ item: Item) -> Bool {
            if item.tier != nil { return item.isMustFix }
            if let must = mustFixKeys { return must.contains(item.key) }
            return true
        }

        /// The gate's must-fix tier as must-fix items (red), each with what
        /// its override needs (TRUST-24).
        var items: [PublishReview.MustFix] {
            if let items = blockerItems, !items.isEmpty {
                return items.filter(isMustFix).map { item in
                    var m = item.mustFix
                    if notOverridable?.contains(m.key) == true { m.overrideAllowed = false }
                    return m
                }
            }
            let pairs = zip(blockerKeys ?? [], blockers ?? []).map { PublishReview.MustFix(key: $0.0, text: $0.1) }
            guard let must = mustFixKeys else { return pairs }
            return pairs.filter { must.contains($0.key) }
        }

        /// The gate's decision tier, each answered in one tap (amber).
        var decisions: [PublishReview.Decision] {
            if let items = blockerItems, !items.isEmpty {
                return items.filter { !isMustFix($0) }.map(\.decision)
            }
            guard let must = mustFixKeys else { return [] }
            return zip(blockerKeys ?? [], blockers ?? []).filter { !must.contains($0.0) }
                .map { PublishCheck.Item(key: $0.0, text: $0.1, tier: "decide").decision }
        }
    }

    /// True once this sheet has sent the week — a send waiting out the
    /// restaurant's window is not sent yet (`queuedSend`).
    var hasSent: Bool { lastResult?.ok == true && lastResult?.queued != true }
    /// A send waiting out the send window, still undoable here (H1).
    var queuedSend: PublishResult? { lastResult?.ok == true && lastResult?.queued == true ? lastResult : nil }
    var isUndoing = false
    /// Said once an Undo stopped the queued send.
    var undoneNote: String?

    /// "Goes to staff in 10 min" — from execute_at when it parses, the
    /// window's length otherwise.
    func queuedLine(_ r: PublishResult, now: Date = Date()) -> String {
        if let at = CavnarISODate.parse(r.executeAt) {
            let mins = max(1, Int((at.timeIntervalSince(now) / 60).rounded(.up)))
            return at > now ? "Goes to staff in \(mins) min" : "Going to staff now"
        }
        if let m = r.undoMinutes, m > 0 { return "Goes to staff in \(m) min" }
        return "Queued to go to staff"
    }

    /// Stops the queued send before its window ends. On success the week is
    /// back to unsent, exactly as before the press.
    func undoQueued() async {
        guard let r = queuedSend, let id = r.actionId, !isUndoing else { return }
        isUndoing = true
        publishError = nil
        defer { isUndoing = false }
        do {
            let u = try await client.undoQueuedAction(id)
            if u.ok {
                Haptic.success()
                lastResult = nil
                undoneNote = u.message ?? "Stopped \u{2014} nothing went to staff."
            } else {
                publishError = u.error ?? "That already went out, or was already undone."
            }
            await load()
        } catch let error as APIClient.APIError {
            publishError = error.message
        } catch {
            publishError = "Couldn\u{2019}t reach Cavnar AI."
        }
    }

    /// `resend` is the owner's explicit "send it to everyone again", behind
    /// its own confirmation. Without it a second call after a successful
    /// send does nothing: the button used to stay "Send to N staff" and a
    /// second tap re-emailed the whole roster (CLIENT-36). The server now
    /// sends it (`resend: "all"`); `missingOnly` sends to the people whose
    /// message failed or who were unreachable then (BUGSLIFE-9).
    func publish(resend: Bool = false, missingOnly: Bool = false) async {
        guard !isPublishing else { return }
        // A week already sent from this sheet goes again only by the owner's
        // explicit resend — or when shifts changed since, and Send tells
        // just those people.
        guard resend || missingOnly || !hasSent || !unsentChanges.isEmpty else { return }
        // A send already waiting out the window is undone, never stacked.
        guard queuedSend == nil else { return }
        guard let scheduleId else {
            publishError = "Open the week you want to send first."
            return
        }
        // Each must-fix fixed or overridden on its own, a hard one with a
        // reason (TRUST-2/3): never one switch for the whole list.
        let items = mustFix
        let resending = resend || missingOnly
        if !resending, !PublishGate.open(items, overrides: overrides).isEmpty {
            publishError = "Fix or override each item above first."
            return
        }
        if !resending, !undecided.isEmpty {
            publishError = "Answer each decision above first."
            return
        }
        let chosen = resending ? [] : PublishGate.overrides(items, overrides)
        let ackKeys = resending ? [] : acknowledgeKeys
        isPublishing = true
        publishError = nil
        undoneNote = nil
        defer { isPublishing = false }
        do {
            let result: PublishResult = try await client.send(
                "/mobile/api/labor/publish-schedule", method: .post,
                body: PublishBody(scheduleId: scheduleId, acknowledge: !ackKeys.isEmpty, keys: ackKeys,
                                  version: resending ? nil : version,
                                  resend: missingOnly ? "missing" : (resend ? "all" : nil),
                                  overrides: chosen))
            lastResult = result
            if result.ok && result.queued {
                // Waiting out the send window: nothing reached staff, so the
                // unsent changes stand (Labor keeps its list) until it goes.
                Haptic.success()
            } else if result.ok {
                Haptic.success()
                overrides = [:]
                acknowledged = []
                gateItems = []
                gateDecisions = []
                refusedOverrides = [:]
                unsentChanges = []
                onSent?()
                if let v = result.version { onVersion?(v) }
            } else {
                publishError = result.error ?? "Couldn't send the schedule."
            }
            await load()
            // Some of the staff are still being told (the outbox job reaches
            // them within the minute): "4 still sending" counts down here and
            // a failure shows, instead of "sent" and silence (BUGSLIFE-21).
            if result.ok, !result.queued {
                if let d = result.delivery { delivery = d }
                if result.stillSending(delivery) > 0 { watchDelivery() }
            }
        } catch let error as APIClient.APIError {
            if error.status == 409, let gate = error.decodeBody(GateResponse.self), gate.needsAck == true {
                // Not a failure: the gate found something not fixed or
                // overridden (it appeared since the list was read), or
                // refused an override (a name not on the roster, "x" for a
                // reason — TRUST-4/5). It joins the list; the overrides
                // already given stand by key, a refused one is said on its
                // item and taken back so it can be given again.
                gateItems = gate.items
                gateDecisions = gate.decisions
                gateNotes = gate.notes?.items ?? []
                refusedOverrides = Dictionary((gate.overrideRefused ?? []).map { ($0.key, $0.why) },
                                              uniquingKeysWith: { a, _ in a })
                for key in refusedOverrides.keys { overrides[key] = nil }
                if let id = gate.scheduleId { self.scheduleId = id }
                await loadReview()
                if let first = gate.overrideRefused?.first {
                    publishError = first.why
                }
                Haptic.warning()
            } else {
                // 403 lands here too — a member who can draft but not send
                // gets the server's own sentence, not a generic one.
                publishError = error.message
            }
        } catch {
            publishError = "Couldn't send the schedule."
        }
    }
}

/// Sends each member of staff their own shifts. The labor module generated
/// schedules and stopped at a CSV download — the people who actually work
/// the shifts never saw it.
struct PublishScheduleSheet: View {
    @State private var viewModel = PublishScheduleViewModel()
    @State private var confirmingResend = false
    /// The whole staff list (closed: only who it can't reach), and the
    /// publish check's notes worth a look (iOS readability round).
    @State private var showingAllStaff = false
    @State private var showingWorthALook = false
    @Environment(\.dismiss) private var dismiss

    /// The schedule_history row to send — required by the server now; the
    /// sheet says so rather than sending without one.
    /// The week's hourly budget, when the screen opening the sheet has it —
    /// the publish check states the hours but not the budget they are held to.
    var hoursBudget: Double? = nil

    init(scheduleId: Int? = nil, unsentChanges: [String] = [], hoursBudget: Double? = nil, version: Int? = nil,
         onSent: (() -> Void)? = nil, onVersion: ((Int) -> Void)? = nil,
         rowActionRunner: ((PublishReview.Action) async -> String?)? = nil,
         versionProvider: (() -> Int?)? = nil, hasUnsavedEdits: Bool = false,
         onOpen: ((ReviewOpenTarget) -> Void)? = nil) {
        self.hoursBudget = hoursBudget
        let vm = PublishScheduleViewModel()
        vm.onOpen = onOpen
        vm.scheduleId = scheduleId
        vm.unsentChanges = unsentChanges
        vm.version = version
        vm.onSent = onSent
        vm.onVersion = onVersion
        vm.rowActionRunner = rowActionRunner
        vm.hasUnsavedEdits = hasUnsavedEdits
        vm.versionProvider = versionProvider
        _viewModel = State(initialValue: vm)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    if viewModel.isLoading && viewModel.contacts.isEmpty {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.6, 0.45])
                    } else if let error = viewModel.errorMessage, viewModel.contacts.isEmpty {
                        Text(error).cavnarText(.body)
                    } else if viewModel.contacts.isEmpty {
                        emptyState
                    } else {
                        if let queued = viewModel.queuedSend {
                            queuedCard(queued)
                        } else if let result = viewModel.lastResult, result.ok {
                            resultCard(result)
                        } else if let note = viewModel.undoneNote {
                            ScheduleNotice(text: note, symbol: "arrow.uturn.backward")
                        }
                        // A copy a newer one replaced is never sent (UI-3):
                        // why, in place of Send.
                        if let why = viewModel.replacedReason {
                            ScheduleNotice(text: why, symbol: "lock.fill")
                        }
                        // The decision, in the order an owner answers it
                        // (TRUST-27): the verdict, what must be fixed, what
                        // to decide, the numbers — then who it reaches.
                        if viewModel.hasUnsavedEdits {
                            ScheduleNotice(text: "Your last edit isn\u{2019}t saved. This checks and sends the week as it was last saved.",
                                           tone: .cavnarAmber, symbol: "exclamationmark.triangle.fill")
                        }
                        let unsent = viewModel.publishedAt == nil
                        let review = unsent ? viewModel.review : nil
                        if let verdict = review?.verdict, !verdict.sentence.isEmpty {
                            PublishVerdictCard(verdict: verdict)
                        }
                        if !viewModel.mustFix.isEmpty {
                            // Red, one card per rule kind with its one fix,
                            // each item fixed or sent past on its own
                            // (TRUST-15, TRUST-24).
                            PublishMustFixCard(items: viewModel.mustFix, groups: review?.mustFixGroups ?? [],
                                               overrides: $viewModel.overrides,
                                               busy: viewModel.actionBusy, notes: viewModel.actionNotes,
                                               refused: viewModel.refusedOverrides) { fix in
                                Task { await viewModel.run(fix) }
                            }
                        }
                        // What holds Send, amber, apart from the suggestions
                        // that never do (TRUST-16).
                        let holding = unsent ? viewModel.holdingDecisions : []
                        if !holding.isEmpty {
                            PublishDecideCard(decisions: holding, groups: review?.decideGroups ?? [],
                                              holds: true, busy: viewModel.actionBusy,
                                              notes: viewModel.actionNotes, answered: viewModel.answered) { option, key in
                                Task { await viewModel.run(option, decision: key) }
                            }
                        }
                        if let review, !review.suggestions.isEmpty {
                            PublishDecideCard(decisions: review.suggestions, groups: [], holds: false,
                                              busy: viewModel.actionBusy,
                                              notes: viewModel.actionNotes, answered: viewModel.answered) { option, key in
                                Task { await viewModel.run(option, decision: key) }
                            }
                        }
                        if viewModel.showsCrew {
                            PublishCrewCard(store: viewModel.crew, rows: nil, addRow: viewModel.crewAddRow)
                        }
                        if let review, !review.alwaysOverridden.isEmpty {
                            PublishAlwaysOverriddenCard(items: review.alwaysOverridden, busy: viewModel.actionBusy,
                                                        notes: viewModel.actionNotes) { action in
                                Task { await viewModel.run(action) }
                            }
                        }
                        if let review, !review.numbers.isEmpty || review.quality.score != nil
                            || review.numbers.laborMissing != nil {
                            PublishNumbersCard(numbers: review.numbers, quality: review.quality,
                                               busy: viewModel.actionBusy, notes: viewModel.actionNotes) { action in
                                Task { await viewModel.run(action) }
                            }
                        }
                        if let review {
                            PublishReviewProof(review: review, busy: viewModel.actionBusy,
                                               notes: viewModel.actionNotes) { action in
                                Task { await viewModel.run(action) }
                            }
                        }
                        staffCard
                        // The week's hours and the rows likely to change —
                        // never a block, behind one tap. With the review the
                        // gate's notes are its "Worth a look" (fyi) above, so
                        // they are not said twice (TRUST-25); without it, the
                        // check's notes stand in.
                        let notes: [PublishNote] = !viewModel.gateNotes.isEmpty ? viewModel.gateNotes
                            : (review == nil ? (viewModel.check?.notes ?? []) : [])
                        let hours = review?.hours ?? viewModel.check?.hours
                        let showsWorth = unsent && (!notes.isEmpty || hours?.hourly != nil)
                        let likely = review?.likelyToChange ?? viewModel.check?.likelyToChange
                        let showsLikely = likely?.ready == true && !(likely?.rows.isEmpty ?? true) && unsent
                        if showsWorth || showsLikely {
                            worthALookDisclosure(notes: notes, hours: hours, showsWorth: showsWorth,
                                                 likely: showsLikely ? likely : nil)
                        }
                        if let error = viewModel.publishError {
                            Text(error)
                                .cavnarText(.secondary, color: .cavnarRedText)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if !viewModel.status.isEmpty {
                            statusCard
                        }
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            // Send pinned in thumb reach, like Labor's own send bar — the
            // one primary on the sheet, never under the staff list.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if !viewModel.contacts.isEmpty && viewModel.replacedReason == nil {
                    CavnarPinnedBar { publishButton }
                }
            }
            .cavnarModuleBackground()
            .navigationTitle("Send to staff")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Send to staff")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Text("Done").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await viewModel.load() }
            .onDisappear { viewModel.stopWatchingDelivery() }
            .onChange(of: viewModel.dismissRequested) { _, now in if now { dismiss() } }
            .sheet(item: $viewModel.editingContact) { contact in
                StaffContactSheet(viewModel: viewModel, contact: contact)
            }
            // "Fix it in Team": the person's record over the review (TRUST-20).
            .sheet(item: $viewModel.personTarget, onDismiss: { Task { await viewModel.refreshGate() } }) { target in
                PersonSheet(target: target)
            }
            // A weekly sales estimate to forecast from (TRUST-29).
            .sheet(isPresented: $viewModel.showingTargets, onDismiss: { Task { await viewModel.refreshGate() } }) {
                AccountTargetsView()
            }
            // A rule or budget the phone sets on the web, over the review.
            .sheet(isPresented: Binding(get: { viewModel.webPath != nil },
                                        set: { if !$0 { viewModel.webPath = nil } }),
                   onDismiss: { Task { await viewModel.refreshGate() } }) {
                if let path = viewModel.webPath, let url = CavnarHandoff.webpageURL(for: path) {
                    CavnarSafariView(url: url).ignoresSafeArea()
                }
            }
        }
    }

    /// "2 notes worth a look · 3 rows likely to change" — the publish
    /// check's notes and the rows the owner usually edits, one tap away.
    private func worthALookDisclosure(notes: [PublishNote], hours: PublishHours?, showsWorth: Bool,
                                      likely: LikelyToChange?) -> some View {
        var parts: [String] = []
        if showsWorth { parts.append(notes.isEmpty ? "The week's hours" : "\(notes.count) worth a look") }
        if let likely { parts.append("\(likely.rows.count) likely to change") }
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.22)) { showingWorthALook.toggle() }
            } label: {
                HStack(spacing: CavnarSpace.xxs + 2) {
                    CavnarMixedText(parts.joined(separator: " \u{00B7} "), role: .label, color: .cavnarEmber2)
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .rotationEffect(.degrees(showingWorthALook ? 180 : 0))
                        .accessibilityHidden(true)
                    Spacer(minLength: 0)
                }
                .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityValue(showingWorthALook ? "Expanded" : "Collapsed")
            if showingWorthALook {
                if showsWorth {
                    PublishWorthALookCard(notes: notes, hours: hours, budget: hoursBudget)
                }
                if let likely {
                    PublishLikelyToChangeCard(likely: likely)
                }
            }
        }
    }

    /// Who it reaches, said once: "11 of 14 reachable", the reach line by
    /// channel, and only the people it can't reach (each opens their
    /// contact); "All 14 people" lists everyone.
    private var staffCard: some View {
        let unreachable = viewModel.contacts.filter { !$0.isReachable }
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack(alignment: .firstTextBaseline) {
                CavnarKicker("This week's staff")
                Spacer()
                CavnarMixedText("\(viewModel.reachableCount) of \(viewModel.contacts.count) reachable",
                                role: .secondary)
            }
            if let week = viewModel.weekLabel {
                Text(week).cavnarText(.secondary)
            }
            // By channel, as the web's reach line: the app, a text they
            // asked for, email — and who only has the portal.
            if let reach = viewModel.reach, reach.total > 0 {
                CavnarMixedText(reach.line, role: .body)
            }
            if !unreachable.isEmpty && !showingAllStaff {
                VStack(spacing: 0) {
                    ForEach(Array(unreachable.enumerated()), id: \.element.id) { index, contact in
                        contactRow(contact)
                        if index < unreachable.count - 1 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        }
                    }
                }
            }
            if showingAllStaff {
                VStack(spacing: 0) {
                    ForEach(Array(viewModel.contacts.enumerated()), id: \.element.id) { index, contact in
                        contactRow(contact)
                        if index < viewModel.contacts.count - 1 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        }
                    }
                }
            }
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.22)) { showingAllStaff.toggle() }
            } label: {
                HStack(spacing: CavnarSpace.xxs + 2) {
                    CavnarMixedText(showingAllStaff ? "Show less" : "All \(viewModel.contacts.count) people",
                                    role: .label, color: .cavnarEmber2)
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .rotationEffect(.degrees(showingAllStaff ? 180 : 0))
                        .accessibilityHidden(true)
                    Spacer(minLength: 0)
                }
                .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityValue(showingAllStaff ? "Expanded" : "Collapsed")
        }
        .cavnarCard()
    }

    private func contactRow(_ contact: StaffContact) -> some View {
        Button {
            Haptic.light()
            viewModel.editingContact = contact
        } label: {
            HStack(spacing: CavnarSpace.s) {
                Circle()
                    .fill(contact.isReachable ? Color.cavnarGreen : Color.cavnarAmber)
                    .frame(width: 7, height: 7)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(contact.employeeName)
                        .cavnarText(.label)
                    Text(contact.reachLine)
                        .cavnarText(.secondary, color: contact.isReachable ? .cavnarInk2 : .cavnarAmber)
                }
                Spacer(minLength: CavnarSpace.xs)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
            }
            .padding(.vertical, CavnarSpace.xs)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// Disabled while any must-fix is neither fixed nor overridden — each
    /// on its own, a hard one with a reason (TRUST-2/3).
    private var sendBlocked: Bool {
        viewModel.contacts.isEmpty || viewModel.isPublishing || !viewModel.canPublish
            || !viewModel.openItems.isEmpty || !viewModel.undecided.isEmpty
            || viewModel.replacedReason != nil
    }

    @ViewBuilder
    private var publishButton: some View {
        if viewModel.replacedReason != nil {
            // Said in the sheet (ScheduleNotice above); nothing to send.
            EmptyView()
        } else if let queued = viewModel.queuedSend {
            // Waiting out the send window (H1): the one action is Undo.
            Button {
                Task { await viewModel.undoQueued() }
            } label: {
                Group {
                    if viewModel.isUndoing {
                        CavnarShimmerText(text: "Stopping\u{2026}")
                    } else {
                        Text(queued.actionId == nil ? viewModel.queuedLine(queued)
                                                    : "Undo \u{00B7} \(viewModel.queuedLine(queued))")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isUndoing || queued.actionId == nil)
        } else if !viewModel.unsentChanges.isEmpty {
            // A week staff already have, changed since: Send tells only
            // the people whose shifts moved.
            Button {
                Task { await viewModel.publish() }
            } label: {
                Group {
                    if viewModel.isPublishing {
                        CavnarShimmerText(text: "Sending…")
                    } else {
                        Text(viewModel.unsentChanges.count == 1
                             ? "Tell \(viewModel.unsentChanges[0]) about the change"
                             : "Tell the \(viewModel.unsentChanges.count) people whose shifts changed")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isPublishing))
            .disabled(viewModel.isPublishing)
        } else if viewModel.hasSent || viewModel.publishedAt != nil {
            // Sent. Another send re-sends to everyone, so it is a separate,
            // quieter action that asks first (CLIENT-36). The people the
            // first send did not reach get their own, unconfirmed action.
            if let missing = viewModel.delivery?.missing, missing > 0 {
                Button {
                    Task { await viewModel.publish(missingOnly: true) }
                } label: {
                    Text(missing == 1 ? "Send to the 1 person who didn\u{2019}t get it"
                                      : "Send to the \(missing) who didn\u{2019}t get it")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.isPublishing)
            }
            Button {
                confirmingResend = true
            } label: {
                Group {
                    if viewModel.isPublishing {
                        CavnarShimmerText(text: "Sending…")
                    } else {
                        Text("Send again to everyone")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isPublishing)
            .confirmationDialog("Send this week to all \(viewModel.reachableCount) again?",
                                isPresented: $confirmingResend, titleVisibility: .visible) {
                Button("Send again") { Task { await viewModel.publish(resend: true) } }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Everyone hears about their shifts a second time, even if nothing changed.")
            }
        } else {
            firstSendButton
        }
    }
    private var firstSendButton: some View {
        Button {
            Task { await viewModel.publish() }
        } label: {
            Group {
                if viewModel.isPublishing {
                    CavnarShimmerText(text: "Sending…")
                } else if !viewModel.canPublish {
                    Text("Your login can draft but not send")
                } else {
                    // "Fix 2 things first", or what Send does — and how many
                    // rules it sends past. Nobody reachable still publishes:
                    // the week goes up in the staff portal and nobody is
                    // notified (the web's "Publish to the staff portal").
                    Text(PublishGate.sendLabel(open: viewModel.openItems.count,
                                               overriding: PublishGate.overrides(viewModel.mustFix,
                                                                                 viewModel.overrides).count,
                                               reachable: viewModel.reachableCount,
                                               serverLabel: viewModel.review?.send.label,
                                               undecided: viewModel.undecided.count))
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sendBlocked))
        .disabled(sendBlocked)
    }

    /// A send waiting out the restaurant's window: nothing has reached
    /// staff, and Undo (the pinned bar) stops it.
    private func queuedCard(_ result: PublishResult) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Queued", icon: "clock")
            CavnarMixedText(viewModel.queuedLine(result), role: .lead)
            Text("Nothing has reached staff yet. Undo stops it; when the window ends, everyone it reaches gets their shifts.")
                .cavnarText(.secondary, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func resultCard(_ result: PublishResult) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let line = result.changesSent == true
                ? "Only the people whose shifts changed were told."
                : (result.alreadyPublished == true
                   ? "This week was already sent to staff — nothing went out twice." : result.note) {
                Text(line)
                    .font(.cavnarBody(CavnarType.secondary, weight: 600))
                    .foregroundStyle(Color.cavnarInk2)
            }
            if result.acknowledged == true {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "hand.raised.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarAmber)
                    Text("Sent after you read the notes")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarAmber)
                }
                .padding(.bottom, 2)
            }
            ForEach(result.sent) { sent in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarGreen)
                    HomeMixedText.make("\(sent.employeeName) — \(sent.shifts) shifts sent",
                                       size: CavnarType.secondary, weight: 600, color: .cavnarInk)
                }
            }
            ForEach(result.unreachable) { person in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarAmber)
                    Text("\(person.employeeName) — \(person.reason)")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(result.failed) { failure in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarRed)
                    Text("\(failure.employeeName) — \(failure.error)")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            // The rest of the staff, still going out (the outbox reaches
            // them within the minute) and what failed since (BUGSLIFE-21).
            if let line = Self.deliveryLine(result, now: viewModel.delivery) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "paperplane")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarInk2)
                        .accessibilityHidden(true)
                    CavnarMixedText(line, role: .secondary, color: .cavnarInk2)
                }
            }
            ForEach(result.staffChangesUndone, id: \.self) { line in
                CavnarMixedText(line, role: .secondary, color: .cavnarAmber)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// "4 still sending · 1 failed — Cavnar AI tries again" — nil when
    /// everyone the send reaches has been told.
    static func deliveryLine(_ result: PublishResult, now: PublishDeliveryCounts?) -> String? {
        let sending = result.stillSending(now)
        let failed = (now ?? result.delivery)?.failed ?? 0
        var parts: [String] = []
        if sending > 0 { parts.append("\(sending) still sending") }
        if failed > 0 { parts.append("\(failed) failed \u{2014} Cavnar AI tries again, and Send again reaches them") }
        if parts.isEmpty { return nil }
        return parts.joined(separator: " \u{00B7} ")
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("WHO'S SEEN IT")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) {
                ForEach(Array(viewModel.status.enumerated()), id: \.element.id) { index, row in
                    HStack(spacing: 10) {
                        Text(row.employeeName)
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarInk)
                        Spacer(minLength: 8)
                        if row.hasViewed {
                            HomeMixedText.make(row.viewCount > 1 ? "Opened \(row.viewCount) times" : "Opened",
                                               size: CavnarType.caption, weight: 700, color: .cavnarGreen)
                        } else {
                            Text("Not opened yet")
                                .font(.cavnarBody(CavnarType.caption, weight: 600))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .padding(.vertical, 10)
                    if index < viewModel.status.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
        .cavnarCard()
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("No schedule yet")
                .font(.cavnarHeadline(CavnarType.emphasis))
                .foregroundStyle(Color.cavnarInk)
            Text("Generate next week's schedule first — then you can send everyone their own shifts.")
                .font(.cavnar(.secondary))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
    }
}

private enum StaffContactField: Hashable, CaseIterable { case email }

private struct StaffContactSheet: View {
    let viewModel: PublishScheduleViewModel
    let contact: StaffContact

    @Environment(\.dismiss) private var dismiss
    @State private var email = ""
    @FocusState private var focusedField: StaffContactField?

    private var canSubmit: Bool {
        !viewModel.isSavingContact && (email.isEmpty || email.contains("@"))
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Where should \(contact.employeeName)'s schedule go? They'll get a private link to their own shifts — no login needed.")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    CavnarFloatingField(
                        icon: "envelope", placeholder: "Email address", text: $email,
                        autocapitalization: .never, focus: $focusedField, field: .email
                    )

                    if let error = viewModel.contactError {
                        Text(error).font(.cavnar(.body)).foregroundStyle(Color.cavnarRedText)
                    }

                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.saveContact(contact.employeeName, email: email) {
                                    dismiss()
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingContact {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Save")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                        .disabled(!canSubmit)

                        Button { dismiss() } label: {
                            Text("Cancel").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle(contact.employeeName)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar(contact.employeeName) }
            .keyboardNavToolbar($focusedField)
        }
        .onAppear {
            email = contact.email
            focusedField = .email
        }
    }
}
