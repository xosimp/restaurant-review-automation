import Foundation

/// The owner side of team operations (parity audit 10/7/26 #10, #26, #62,
/// #67, #71): the Team inbox, the lineup brief, the staff pulse, team
/// messages, house rules, docs and certifications. Every shape here is the
/// server's own (staff_comms_routes, staff_knowledge_routes,
/// strategy_routes, mobile_api) decoded leniently: a key an older server
/// leaves out reads as missing, never as a zero.

extension KeyedDecodingContainer {
    /// The value at `key`, or nil when it is absent, null or another shape.
    func teamValue<T: Decodable>(_ key: Key) -> T? {
        (try? decodeIfPresent(T.self, forKey: key)) ?? nil
    }

    /// An integer sent as a number or as its text.
    func teamInt(_ key: Key) -> Int? {
        if let n: Int = teamValue(key) { return n }
        if let d: Double = teamValue(key) { return Int(d) }
        if let s: String = teamValue(key) { return Int(s.trimmingCharacters(in: .whitespaces)) }
        return nil
    }

    /// A figure sent as a number or as its text.
    func teamDouble(_ key: Key) -> Double? {
        if let d: Double = teamValue(key) { return d }
        if let s: String = teamValue(key) { return Double(s) }
        return nil
    }

    /// A list that keeps every element it can read and skips the rest.
    func teamList<T: Decodable>(_ key: Key) -> [T] {
        ((try? decodeIfPresent(HomeLenientListDecodable<T>.self, forKey: key)) ?? nil)?.items ?? []
    }
}

// MARK: - Team inbox (staff_comms; /mobile/api/labor/inbox*)

/// One employee's thread with the managers (staff_comms.manager_inbox).
struct TeamInboxThread: Decodable, Identifiable, Equatable {
    let threadId: Int
    let membershipId: Int?
    let employeeName: String
    let lastBody: String
    /// "staff" or "manager" — who wrote the last message.
    let lastFrom: String
    let lastShiftDate: String?
    let lastAt: String?
    let unread: Int
    var id: Int { threadId }

    enum CodingKeys: String, CodingKey {
        case unread
        case threadId = "thread_id", membershipId = "membership_id", employeeName = "employee_name"
        case lastBody = "last_body", lastFrom = "last_from", lastShiftDate = "last_shift_date", lastAt = "last_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.teamInt(.threadId) else {
            throw DecodingError.dataCorruptedError(forKey: .threadId, in: c, debugDescription: "no thread id")
        }
        threadId = id
        membershipId = c.teamInt(.membershipId)
        employeeName = c.teamValue(.employeeName) ?? "Staff"
        lastBody = c.teamValue(.lastBody) ?? ""
        lastFrom = c.teamValue(.lastFrom) ?? ""
        lastShiftDate = c.teamValue(.lastShiftDate)
        lastAt = c.teamValue(.lastAt)
        unread = c.teamInt(.unread) ?? 0
    }

    /// "You: See you at 4" for the manager's side, the message itself for theirs.
    var preview: String {
        let body = lastBody.replacingOccurrences(of: "\n", with: " ")
        return (lastFrom == "manager" ? "You: " : "") + String(body.prefix(140))
    }
}

/// Someone running late today (staff_comms.late_today).
struct TeamLateReport: Decodable, Identifiable, Equatable {
    let id: Int
    let employeeName: String
    let shiftStart: String
    let role: String?
    let etaMinutes: Int?
    let note: String?
    let expectedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, role, note
        case employeeName = "employee_name", shiftStart = "shift_start", etaMinutes = "eta_minutes"
        case expectedAt = "expected_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.teamInt(.id) ?? 0
        employeeName = c.teamValue(.employeeName) ?? "Someone"
        shiftStart = c.teamValue(.shiftStart) ?? ""
        role = c.teamValue(.role)
        etaMinutes = c.teamInt(.etaMinutes)
        note = c.teamValue(.note)
        expectedAt = c.teamValue(.expectedAt)
    }

    /// "Dana is running about 20 minutes late" — the web's sentence.
    var headline: String {
        guard let eta = etaMinutes else { return "\(employeeName) is running late" }
        return "\(employeeName) is running about \(eta) minutes late"
    }

    /// "4:00pm Server · expect them around 4:20pm · “bus is slow”".
    var detail: String {
        var bits = [([shiftStart, role ?? ""].filter { !$0.isEmpty }).joined(separator: " ")]
        if let expectedAt, !expectedAt.isEmpty { bits.append("expect them around \(expectedAt)") }
        if let note, !note.isEmpty { bits.append("\u{201C}\(note)\u{201D}") }
        return bits.filter { !$0.isEmpty }.joined(separator: " \u{00B7} ")
    }
}

struct TeamInboxResponse: Decodable {
    let ok: Bool
    let error: String?
    let threads: [TeamInboxThread]
    let unread: Int
    let lateToday: [TeamLateReport]

    enum CodingKeys: String, CodingKey {
        case ok, error, threads, unread
        case lateToday = "late_today"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        threads = c.teamList(.threads)
        unread = c.teamInt(.unread) ?? 0
        lateToday = c.teamList(.lateToday)
    }
}

/// One message in a staff thread (staff_comms._msg_out).
struct TeamThreadMessage: Decodable, Identifiable, Equatable {
    let id: Int
    /// "staff" or "manager".
    let from: String
    let senderName: String
    let body: String
    let shiftDate: String?
    let createdAt: String?
    /// For a manager's message: when the employee read it ("Seen").
    let readAt: String?

    enum CodingKeys: String, CodingKey {
        case id, from, body
        case senderName = "sender_name", shiftDate = "shift_date", createdAt = "created_at", readAt = "read_at"
    }

    init(id: Int, from: String, senderName: String, body: String, shiftDate: String? = nil,
         createdAt: String? = nil, readAt: String? = nil) {
        self.id = id
        self.from = from
        self.senderName = senderName
        self.body = body
        self.shiftDate = shiftDate
        self.createdAt = createdAt
        self.readAt = readAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.teamInt(.id) ?? 0
        from = c.teamValue(.from) ?? "staff"
        senderName = c.teamValue(.senderName) ?? ""
        body = c.teamValue(.body) ?? ""
        shiftDate = c.teamValue(.shiftDate)
        createdAt = c.teamValue(.createdAt)
        readAt = c.teamValue(.readAt)
    }

    var isManager: Bool { from == "manager" }
}

struct TeamThreadResponse: Decodable {
    struct Thread: Decodable {
        let threadId: Int?
        let employeeName: String?
        enum CodingKeys: String, CodingKey {
            case threadId = "thread_id", employeeName = "employee_name"
        }
    }
    let ok: Bool
    let error: String?
    let thread: Thread?
    let messages: [TeamThreadMessage]

    enum CodingKeys: String, CodingKey { case ok, error, thread, messages }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        thread = c.teamValue(.thread)
        messages = c.teamList(.messages)
    }
}

/// POST /labor/inbox/threads/<id>/reply's answer.
struct TeamReplyResponse: Decodable {
    let ok: Bool
    let error: String?
    let message: TeamThreadMessage?
    /// "push" | "sms" | "email" | nil — nil: they see it next time they open the app.
    let deliveredVia: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, message
        case deliveredVia = "delivered_via"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        message = c.teamValue(.message)
        deliveredVia = c.teamValue(.deliveredVia)
    }
}

/// {body} — the one key the reply route reads.
struct TeamReplyBody: Encodable, Equatable {
    let body: String
}

/// A manager's post to staff with who has read it (staff_comms._announcement_out).
struct TeamAnnouncement: Decodable, Identifiable, Equatable {
    struct Reader: Decodable, Equatable {
        let name: String
        let ackedAt: String?
        enum CodingKeys: String, CodingKey { case name; case ackedAt = "acked_at" }
    }
    let id: Int
    let title: String
    let body: String
    let priority: String
    let audienceLabel: String
    let expiresOn: String?
    let expired: Bool
    let withdrawn: Bool
    let createdAt: String?
    let createdByName: String
    let recipients: Int
    let read: Int
    let unreadNames: [String]
    let readBy: [Reader]

    enum CodingKeys: String, CodingKey {
        case id, title, body, priority, expired, withdrawn, recipients, read
        case audienceLabel = "audience_label", expiresOn = "expires_on", createdAt = "created_at"
        case createdByName = "created_by_name", unreadNames = "unread_names", readBy = "read_by"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.teamInt(.id) ?? 0
        title = c.teamValue(.title) ?? ""
        body = c.teamValue(.body) ?? ""
        priority = c.teamValue(.priority) ?? "normal"
        audienceLabel = c.teamValue(.audienceLabel) ?? "Everyone"
        expiresOn = c.teamValue(.expiresOn)
        expired = c.teamValue(.expired) ?? false
        withdrawn = c.teamValue(.withdrawn) ?? false
        createdAt = c.teamValue(.createdAt)
        createdByName = c.teamValue(.createdByName) ?? ""
        recipients = c.teamInt(.recipients) ?? 0
        read = c.teamInt(.read) ?? 0
        unreadNames = c.teamValue(.unreadNames) ?? []
        readBy = c.teamList(.readBy)
    }

    var isUrgent: Bool { priority == "urgent" }
    /// Withdraw is offered while the post is still in inboxes.
    var canWithdraw: Bool { !withdrawn && !expired }
    /// "3 of 7 read".
    var readLine: String { "\(read) of \(recipients) read" }
}

struct TeamAnnouncementsResponse: Decodable {
    let ok: Bool
    let error: String?
    let announcements: [TeamAnnouncement]
    let roles: [String]

    enum CodingKeys: String, CodingKey { case ok, error, announcements, roles }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        announcements = c.teamList(.announcements)
        roles = c.teamValue(.roles) ?? []
    }
}

/// POST /labor/inbox/announcements — every key create_announcement reads,
/// each sent even when empty (null), so nothing defaults by omission.
struct TeamAnnounceBody: Encodable, Equatable {
    enum Audience: String, CaseIterable, Identifiable {
        case all, role, shiftDate = "shift_date"
        var id: String { rawValue }
        var label: String {
            switch self {
            case .all: return "Everyone"
            case .role: return "One role"
            case .shiftDate: return "One day\u{2019}s schedule"
            }
        }
    }

    let title: String
    let body: String
    let priority: String
    let audience: String
    let audienceValue: String?
    let expiresOn: String?

    enum CodingKeys: String, CodingKey {
        case title, body, priority, audience
        case audienceValue = "audience_value", expiresOn = "expires_on"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(title, forKey: .title)
        try c.encode(body, forKey: .body)
        try c.encode(priority, forKey: .priority)
        try c.encode(audience, forKey: .audience)
        if let audienceValue { try c.encode(audienceValue, forKey: .audienceValue) } else { try c.encodeNil(forKey: .audienceValue) }
        if let expiresOn { try c.encode(expiresOn, forKey: .expiresOn) } else { try c.encodeNil(forKey: .expiresOn) }
    }

    /// The body for what the composer holds: a role or a day only for the
    /// audience that reads it, a blank last day as null.
    static func make(title: String, body: String, urgent: Bool, audience: Audience, role: String,
                     day: String, expires: String) -> TeamAnnounceBody {
        let value: String?
        switch audience {
        case .all: value = nil
        case .role: value = role.isEmpty ? nil : role
        case .shiftDate: value = day.isEmpty ? nil : day
        }
        return TeamAnnounceBody(title: title.trimmingCharacters(in: .whitespacesAndNewlines),
                                body: body.trimmingCharacters(in: .whitespacesAndNewlines),
                                priority: urgent ? "urgent" : "normal", audience: audience.rawValue,
                                audienceValue: value, expiresOn: expires.isEmpty ? nil : expires)
    }
}

struct TeamAnnounceResponse: Decodable {
    let ok: Bool
    let error: String?
    let announcement: TeamAnnouncement?
    /// {"push", "sms", "email", "none"} → how many each way.
    let delivery: [String: Int]

    enum CodingKeys: String, CodingKey { case ok, error, announcement, delivery }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        announcement = c.teamValue(.announcement)
        delivery = c.teamValue(.delivery) ?? [:]
    }

    /// "Sent to 7 people · 2 see it next time they open the app" — the web's line.
    var sentLine: String? {
        guard let a = announcement else { return nil }
        let reached = (delivery["push"] ?? 0) + (delivery["sms"] ?? 0) + (delivery["email"] ?? 0)
        var line = "Sent to \(a.recipients) " + (a.recipients == 1 ? "person" : "people")
        if reached < a.recipients { line += " \u{00B7} \(a.recipients - reached) see it next time they open the app" }
        return line
    }
}

struct TeamWithdrawResponse: Decodable {
    let ok: Bool
    let error: String?
    let announcement: TeamAnnouncement?
}

// MARK: - Lineup brief (staff_brief; /mobile/api/staff-brief*)

struct LineupBrief: Decodable, Equatable {
    struct Item: Decodable, Equatable, Hashable {
        let kind: String?
        let text: String
    }
    struct Focus: Decodable, Equatable {
        let item: String
        let line: String?
    }
    struct Suggestion: Decodable, Equatable, Hashable {
        let item: String
        let why: String?
    }

    let day: String
    let weekday: String
    let items: [Item]
    let draftText: String?
    let draftStatus: String
    let draftItemsChanged: Bool
    let modelUsedToday: Bool
    let approvedText: String?
    let edited: Bool
    let focus: Focus?
    let suggestions: [Suggestion]

    enum CodingKeys: String, CodingKey {
        case day, weekday, items, edited, focus, suggestions
        case draftText = "draft_text", draftStatus = "draft_status", draftItemsChanged = "draft_items_changed"
        case modelUsedToday = "model_used_today", approvedText = "approved_text"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = c.teamValue(.day) ?? ""
        weekday = c.teamValue(.weekday) ?? ""
        items = c.teamList(.items)
        draftText = c.teamValue(.draftText)
        draftStatus = c.teamValue(.draftStatus) ?? "none"
        draftItemsChanged = c.teamValue(.draftItemsChanged) ?? false
        modelUsedToday = c.teamValue(.modelUsedToday) ?? false
        approvedText = c.teamValue(.approvedText)
        edited = c.teamValue(.edited) ?? false
        focus = c.teamValue(.focus)
        suggestions = c.teamList(.suggestions)
    }

    var isApproved: Bool { !(approvedText ?? "").isEmpty }
    var hasDraft: Bool { !(draftText ?? "").isEmpty }
    /// A draft is waiting on the manager: written, not yet approved.
    var isWaiting: Bool { hasDraft && !isApproved }
    /// "Draft it for me" — the day's one model call is unspent, there is
    /// no draft, and there are lines enough to join (staff_brief.REWRITE_MIN_ITEMS).
    var canAskForDraft: Bool { !modelUsedToday && !hasDraft && items.count > 1 }

    /// What the AI draft's state means — the web's DRAFT_SAID, word for word.
    var draftSaid: String {
        if isApproved { return edited ? "Your words are what the team sees." : "The team sees this brief." }
        switch draftStatus {
        case "drafted": return "The AI drafted this from the lines above. Edit it if you like, then approve it."
        case "refused": return "The AI draft didn\u{2019}t pass the staff check, so it isn\u{2019}t offered. The lines above stand."
        case "held": return "No AI draft today: the data it rests on isn\u{2019}t current. The lines above stand."
        case "failed": return "The AI draft didn\u{2019}t come back today. The lines above stand."
        case "no_items": return "Nothing for the AI to draft from today."
        default: return "No AI draft today. Approve the lines as a brief, or write your own."
        }
    }
}

struct LineupBriefResponse: Decodable {
    let ok: Bool
    let error: String?
    let canEdit: Bool
    let brief: LineupBrief?

    enum CodingKeys: String, CodingKey {
        case ok, error, brief
        case canEdit = "can_edit"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        canEdit = c.teamValue(.canEdit) ?? false
        brief = c.teamValue(.brief)
    }
}

/// The staff-brief write bodies — `day` always (a write names today or
/// tomorrow), `text` for approve: the manager's words, or nil (null) to
/// approve the stored draft as written (the push's Approve).
struct LineupApproveBody: Encodable, Equatable {
    let day: String
    let text: String?
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(day, forKey: .day)
        if let text { try c.encode(text, forKey: .text) } else { try c.encodeNil(forKey: .text) }
    }
    enum CodingKeys: String, CodingKey { case day, text }
}

struct LineupDayBody: Encodable, Equatable {
    let day: String
}

struct LineupFocusBody: Encodable, Equatable {
    let day: String
    let item: String
    let line: String
}

// MARK: - Staff pulse (staff_insights; /mobile/api/labor/staff-pulse)

struct StaffPulseSummary: Decodable, Equatable {
    struct Day: Decodable, Equatable, Identifiable {
        let date: String
        let responses: Int
        let average: Double?
        var id: String { date }
        enum CodingKeys: String, CodingKey { case date, responses, average }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            date = c.teamValue(.date) ?? ""
            responses = c.teamInt(.responses) ?? 0
            average = c.teamDouble(.average)
        }
    }
    struct Note: Decodable, Equatable, Hashable {
        let text: String
        let dateLabel: String?
        enum CodingKeys: String, CodingKey { case text; case dateLabel = "date_label" }
    }
    struct Window: Decodable, Equatable { let label: String? }

    let days: Int
    let window: Window?
    let responses: Int
    let minResponses: Int
    let enough: Bool
    /// Nil below the floor — never shown as 0.
    let average: Double?
    let distribution: [String: Int]
    let byDay: [Day]
    let otherDaysResponses: Int
    let notes: [Note]
    let message: String?

    enum CodingKeys: String, CodingKey {
        case days, window, responses, enough, average, distribution, notes, message
        case minResponses = "min_responses", byDay = "by_day", otherDaysResponses = "other_days_responses"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        days = c.teamInt(.days) ?? 14
        window = c.teamValue(.window)
        responses = c.teamInt(.responses) ?? 0
        minResponses = c.teamInt(.minResponses) ?? 3
        // Enough only when the server says so and sent the figure.
        average = c.teamDouble(.average)
        enough = (c.teamValue(.enough) ?? false) && average != nil
        distribution = c.teamValue(.distribution) ?? [:]
        byDay = c.teamList(.byDay)
        otherDaysResponses = c.teamInt(.otherDaysResponses) ?? 0
        notes = c.teamList(.notes)
        message = c.teamValue(.message)
    }

    /// "4.2" — one decimal, "—" when there is no figure.
    var averageText: String { average.map { String(format: "%.1f", $0) } ?? "\u{2014}" }

    /// The count sentence shown below the floor — the server's, or the same words.
    var floorLine: String {
        if let message, !message.isEmpty { return message }
        return "\(responses) answer\(responses == 1 ? "" : "s") so far. Shown once \(minResponses) people have "
            + "answered, so nobody\u{2019}s answer is singled out."
    }
}

struct StaffPulseResponse: Decodable {
    let ok: Bool
    let summary: StaffPulseSummary?
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        summary = ok ? try? StaffPulseSummary(from: decoder) : nil
    }
    enum CodingKeys: String, CodingKey { case ok }
}

/// The close-out's `staff_pulse` (staff_insights.pulse_for_closeout):
/// present only once enough staff answered for the night.
struct CloseOutStaffPulse: Decodable, Equatable {
    let responses: Int
    let average: Double?
    let line: String
}

// MARK: - Team messages (models.team_messages; /mobile/api/team/*)

/// Another console login at this restaurant, with the last exchange.
struct TeammateThread: Decodable, Identifiable, Equatable {
    let userId: Int
    let name: String
    let role: String?
    let lastMessage: String?
    let lastFromMe: Bool
    let lastAt: String?
    let unread: Int
    var id: Int { userId }

    enum CodingKeys: String, CodingKey {
        case name, username, role, unread
        case userId = "user_id", lastMessage = "last_message", lastFromMe = "last_from_me", lastAt = "last_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.teamInt(.userId) else {
            throw DecodingError.dataCorruptedError(forKey: .userId, in: c, debugDescription: "no user id")
        }
        userId = id
        let username: String = c.teamValue(.username) ?? ""
        let named: String = c.teamValue(.name) ?? ""
        name = named.isEmpty ? (username.isEmpty ? "Teammate" : username) : named
        role = c.teamValue(.role)
        lastMessage = c.teamValue(.lastMessage)
        lastFromMe = c.teamValue(.lastFromMe) ?? false
        lastAt = c.teamValue(.lastAt)
        unread = c.teamInt(.unread) ?? 0
    }

    var initials: String {
        let parts = name.split(separator: " ").prefix(2)
        let s = parts.compactMap { $0.first.map(String.init) }.joined().uppercased()
        return s.isEmpty ? "?" : s
    }
}

struct TeamMessagesInboxResponse: Decodable {
    let ok: Bool
    let error: String?
    let teammates: [TeammateThread]
    let unreadTotal: Int

    enum CodingKeys: String, CodingKey {
        case ok, error, teammates
        case unreadTotal = "unread_total"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        teammates = c.teamList(.teammates)
        unreadTotal = c.teamInt(.unreadTotal) ?? 0
    }
}

struct TeamDirectMessage: Decodable, Identifiable, Equatable {
    let id: Int
    let senderId: Int
    let recipientId: Int
    let body: String
    let readAt: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, body
        case senderId = "sender_id", recipientId = "recipient_id", readAt = "read_at", createdAt = "created_at"
    }

    init(id: Int, senderId: Int, recipientId: Int, body: String, readAt: String? = nil, createdAt: String? = nil) {
        self.id = id
        self.senderId = senderId
        self.recipientId = recipientId
        self.body = body
        self.readAt = readAt
        self.createdAt = createdAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.teamInt(.id) ?? 0
        senderId = c.teamInt(.senderId) ?? 0
        recipientId = c.teamInt(.recipientId) ?? 0
        body = c.teamValue(.body) ?? ""
        readAt = c.teamValue(.readAt)
        createdAt = c.teamValue(.createdAt)
    }
}

struct TeamDirectThreadResponse: Decodable {
    let ok: Bool
    let error: String?
    let messages: [TeamDirectMessage]
    let myId: Int?

    enum CodingKeys: String, CodingKey {
        case ok, error, messages
        case myId = "my_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        messages = c.teamList(.messages)
        myId = c.teamInt(.myId)
    }
}

/// POST /mobile/api/team/messages — {recipient_id, body}.
struct TeamDirectSendBody: Encodable, Equatable {
    let recipientId: Int
    let body: String
    enum CodingKeys: String, CodingKey {
        case body
        case recipientId = "recipient_id"
    }
}

struct TeamDirectSendResponse: Decodable {
    let ok: Bool
    let error: String?
    let message: TeamDirectMessage?
}

// MARK: - House rules, staff docs, certifications (staff_knowledge)

struct KnowledgeDoc: Decodable, Identifiable, Equatable {
    let id: Int
    let kind: String
    let kindLabel: String
    let title: String
    let body: String
    let roles: [String]
    let updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, title, body, roles
        case kindLabel = "kind_label", updatedAt = "updated_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.teamInt(.id) ?? 0
        kind = c.teamValue(.kind) ?? "other"
        kindLabel = c.teamValue(.kindLabel) ?? kind.replacingOccurrences(of: "_", with: " ").capitalized
        title = c.teamValue(.title) ?? ""
        body = c.teamValue(.body) ?? ""
        roles = c.teamValue(.roles) ?? []
        updatedAt = c.teamValue(.updatedAt)
    }

    /// "Menu spec · Server, Bartender · updated 10/1/26".
    var metaLine: String {
        var bits = [kindLabel, roles.isEmpty ? "Everyone" : roles.joined(separator: ", ")]
        if let updatedAt, !updatedAt.isEmpty { bits.append("updated " + CavnarDate.mdy(updatedAt)) }
        return bits.joined(separator: " \u{00B7} ")
    }
}

struct HouseRulesResponse: Decodable {
    let ok: Bool
    let error: String?
    let canEdit: Bool
    let houseRules: KnowledgeDoc?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case canEdit = "can_edit", houseRules = "house_rules"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        canEdit = c.teamValue(.canEdit) ?? false
        houseRules = c.teamValue(.houseRules)
    }
}

/// POST /house-rules — {title, body}.
struct HouseRulesBody: Encodable, Equatable {
    var title = "House rules"
    let body: String
}

struct StaffDocKind: Decodable, Identifiable, Equatable, Hashable {
    let kind: String
    let label: String
    var id: String { kind }
}

struct StaffDocsResponse: Decodable {
    let ok: Bool
    let error: String?
    let canEdit: Bool
    let kinds: [StaffDocKind]
    let docs: [KnowledgeDoc]

    enum CodingKeys: String, CodingKey {
        case ok, error, kinds, docs
        case canEdit = "can_edit"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        canEdit = c.teamValue(.canEdit) ?? false
        kinds = c.teamList(.kinds)
        docs = c.teamList(.docs)
    }
}

/// POST /staff-docs — {id (null for a new doc), kind, title, body, roles}.
struct StaffDocSaveBody: Encodable, Equatable {
    let id: Int?
    let kind: String
    let title: String
    let body: String
    let roles: [String]

    enum CodingKeys: String, CodingKey { case id, kind, title, body, roles }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        if let id { try c.encode(id, forKey: .id) } else { try c.encodeNil(forKey: .id) }
        try c.encode(kind, forKey: .kind)
        try c.encode(title, forKey: .title)
        try c.encode(body, forKey: .body)
        try c.encode(roles, forKey: .roles)
    }

    /// "Server, Bartender" → ["Server", "Bartender"]; blank is everyone.
    static func roles(from text: String) -> [String] {
        text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }
}

struct KnowledgeCert: Decodable, Identifiable, Equatable {
    let id: Int
    let employeeName: String
    let cert: String
    let expiresOn: String?
    let issuedOn: String?
    let note: String?
    let daysLeft: Int?
    /// "expired" | "expiring" | "current" | "no_expiry".
    let status: String

    enum CodingKeys: String, CodingKey {
        case id, cert, note, status
        case employeeName = "employee_name", expiresOn = "expires_on", issuedOn = "issued_on", daysLeft = "days_left"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.teamInt(.id) ?? 0
        employeeName = c.teamValue(.employeeName) ?? ""
        cert = c.teamValue(.cert) ?? ""
        expiresOn = c.teamValue(.expiresOn)
        issuedOn = c.teamValue(.issuedOn)
        note = c.teamValue(.note)
        daysLeft = c.teamInt(.daysLeft)
        status = c.teamValue(.status) ?? "no_expiry"
    }

    /// "food handler" — the web's label.
    var certLabel: String { cert.replacingOccurrences(of: "_", with: " ") }

    /// "Expires 11/2/26" / "Expired 9/30/26" / "No expiry date" — the web's certSaid.
    var expiryLine: String {
        guard let expiresOn, !expiresOn.isEmpty, status != "no_expiry" else { return "No expiry date" }
        return (status == "expired" ? "Expired " : "Expires ") + CavnarDate.mdy(expiresOn)
    }
}

struct StaffCertsResponse: Decodable {
    let ok: Bool
    let error: String?
    let canEdit: Bool
    let remindDays: Int?
    let certs: [KnowledgeCert]
    let roster: [String]

    enum CodingKeys: String, CodingKey {
        case ok, error, certs, roster
        case canEdit = "can_edit", remindDays = "remind_days"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        canEdit = c.teamValue(.canEdit) ?? false
        remindDays = c.teamInt(.remindDays)
        certs = c.teamList(.certs)
        roster = c.teamValue(.roster) ?? []
    }
}

/// POST /staff-certs — every key save_cert reads; an upsert on person +
/// certificate that REPLACES issued_on and note, so they are always sent.
struct StaffCertSaveBody: Encodable, Equatable {
    let employeeName: String
    let cert: String
    let expiresOn: String?
    let issuedOn: String?
    let note: String?

    enum CodingKeys: String, CodingKey {
        case cert, note
        case employeeName = "employee_name", expiresOn = "expires_on", issuedOn = "issued_on"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(employeeName, forKey: .employeeName)
        try c.encode(cert, forKey: .cert)
        for (key, value) in [(CodingKeys.expiresOn, expiresOn), (.issuedOn, issuedOn), (.note, note)] {
            if let value, !value.isEmpty { try c.encode(value, forKey: key) } else { try c.encodeNil(forKey: key) }
        }
    }
}

// MARK: - People: rename, merge, undo, erase (strategy_routes people twins)

struct PersonRenameBody: Encodable, Equatable { let name: String }
struct PersonEraseBody: Encodable, Equatable { let confirm: String }
struct PeopleMergeBody: Encodable, Equatable {
    let from: String
    let into: String
}

struct PersonRenameResponse: Decodable {
    let ok: Bool
    let error: String?
    let to: String?
    let key: String?
}

struct PeopleMergeResponse: Decodable {
    let ok: Bool
    let error: String?
    let from: String?
    let into: String?
}

/// One merge of the last people.UNMERGE_DAYS days.
struct PeopleMerge: Decodable, Identifiable, Equatable {
    let mergeId: Int
    let from: String
    let into: String
    let mergedOn: String?
    let undoUntil: String?
    let undoable: Bool
    let whyNot: String?
    let undone: Bool
    var id: Int { mergeId }

    enum CodingKeys: String, CodingKey {
        case from, into, undoable, undone
        case mergeId = "merge_id", mergedOn = "merged_on", undoUntil = "undo_until", whyNot = "why_not"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mergeId = c.teamInt(.mergeId) ?? 0
        from = c.teamValue(.from) ?? ""
        into = c.teamValue(.into) ?? ""
        mergedOn = c.teamValue(.mergedOn)
        undoUntil = c.teamValue(.undoUntil)
        undoable = c.teamValue(.undoable) ?? false
        whyNot = c.teamValue(.whyNot)
        undone = c.teamValue(.undone) ?? false
    }
}

struct PeopleMergesResponse: Decodable {
    let ok: Bool
    let error: String?
    let merges: [PeopleMerge]
    let canUndo: Bool

    enum CodingKeys: String, CodingKey {
        case ok, error, merges
        case canUndo = "can_undo"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        error = c.teamValue(.error)
        merges = c.teamList(.merges)
        canUndo = c.teamValue(.canUndo) ?? false
    }
}

/// A row of GET /people — who someone can be merged into.
struct PeopleListRow: Decodable, Identifiable, Equatable, Hashable {
    let key: String
    let name: String
    let active: Bool
    var id: String { key }

    enum CodingKeys: String, CodingKey { case key, name, active }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = c.teamValue(.key) ?? ""
        name = c.teamValue(.name) ?? ""
        active = c.teamValue(.active) ?? true
    }
}

struct PeopleListResponse: Decodable {
    let ok: Bool
    let people: [PeopleListRow]
    enum CodingKeys: String, CodingKey { case ok, people }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.teamValue(.ok) ?? false
        people = c.teamList(.people)
    }
}

/// `{}` — the body of a POST whose route reads nothing from it.
struct TeamEmptyBody: Encodable, Equatable {}

/// A path segment for a person key or any id built into a URL.
enum TeamPath {
    static func segment(_ raw: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return raw.addingPercentEncoding(withAllowedCharacters: allowed) ?? raw
    }
}

/// Times the way the house writes them: "Today · 6:45pm", "10/3/26 · 6:45pm".
enum TeamTime {
    static func when(_ stamp: String?) -> String {
        guard let stamp, let date = CavnarDate.timestamp(stamp) else { return stamp.map(CavnarDate.mdy) ?? "" }
        if Calendar.current.isDateInToday(date) { return "Today \u{00B7} " + CavnarDate.time(date) }
        if Calendar.current.isDateInYesterday(date) { return "Yesterday \u{00B7} " + CavnarDate.time(date) }
        return CavnarDate.mdyTime(date)
    }
}
