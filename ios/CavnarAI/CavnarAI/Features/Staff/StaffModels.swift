import Foundation

/// The employee tier's payloads. These deliberately mirror the web portal's
/// JSON one-for-one (staff_routes.py) rather than getting their own mobile
/// endpoints — two clients reading the same routes cannot drift into showing
/// different people or different shifts.

struct StaffRosterEntry: Decodable, Identifiable, Hashable {
    let membershipID: Int
    let name: String

    var id: Int { membershipID }

    enum CodingKeys: String, CodingKey {
        case membershipID = "membership_id"
        case name
    }
}

struct StaffRosterResponse: Decodable {
    let ok: Bool
    let restaurant: String?
    let roster: [StaffRosterEntry]?
    let error: String?
    /// A one-shot token the sign-in POST must carry. The server spends it on
    /// read, so a captured sign-in request cannot be replayed.
    let loginNonce: String?

    enum CodingKeys: String, CodingKey {
        case ok, restaurant, roster, error
        case loginNonce = "login_nonce"
    }
}

struct StaffLoginResponse: Decodable {
    let ok: Bool
    /// The shift session. The web client gets this as an httponly cookie; a
    /// native client has no cookie jar, so the same value comes back in the
    /// body and goes straight to the Keychain.
    let token: String?
    let error: String?
    let locked: Bool?
    /// True when the nonce had already been spent or aged out — recoverable,
    /// unlike a wrong PIN: refetch the roster and send the same PIN again.
    let nonceExpired: Bool?
    /// A replacement nonce, so a mistyped PIN does not cost a round trip.
    let loginNonce: String?

    enum CodingKeys: String, CodingKey {
        case ok, token, error, locked
        case nonceExpired = "nonce_expired"
        case loginNonce = "login_nonce"
    }
}

/// The live request on one shift (staff_schedule._leg, employee audit C6):
/// a drop or swap this person asked for, or a shift the manager posted
/// open. `status` is "pending", "approved" or "open" — "open" means
/// approved and still theirs until somebody picks it up (C8).
struct StaffShiftRequestState: Codable, Hashable {
    let id: Int
    let kind: String
    let status: String

    /// The chip on the shift: "Giving up", "Swap", "Open".
    var chipLabel: String {
        if status == "open" { return "Open" }
        switch kind {
        case "swap": return "Swap"
        case "post": return "Posted open"
        default: return "Giving up"
        }
    }

    /// The sentence under the chip — what the state means for this person.
    var detail: String {
        if status == "open" {
            return kind == "post"
                ? "Your manager posted this shift open. You\u{2019}re still on it until someone picks it up."
                : "Approved. You\u{2019}re still on it until someone picks it up."
        }
        switch (kind, status) {
        case ("swap", "approved"): return "Your manager approved the swap. Waiting on your colleague."
        case ("swap", _): return "Swap asked. Waiting on your manager and your colleague."
        default: return "You asked to give this up. Waiting on your manager."
        }
    }

    /// "Open" still has the person on the shift: a watch, not a done deal.
    var isWatch: Bool { status == "open" }
}

struct StaffShift: Codable, Hashable {
    let date: String?
    let day: String?
    let role: String?
    let shiftStart: String?
    let shiftEnd: String?
    let scheduledHours: String?
    let notes: String?
    /// The kitchen station the draft put this cook on ("Grill", or "Prep then
    /// Grill" across a double) — kitchen_stations, 9/30/26. Nil off the line.
    let station: String?
    /// The dining-room section a manager put this shift in ("Patio" —
    /// models.shift_sections, employee audit V12). Absent when none.
    let section: String?
    /// The meal window ("3:30pm–4:00pm", labor.break_window); empty or nil
    /// when the shift is short or the rule is off.
    let breakWindow: String?
    /// `hours` as the number the server sent (6.5), when it sent one.
    let hours: Double?
    /// The live request on this leg, or nil (C6).
    let request: StaffShiftRequestState?
    /// ["drop", "swap"] while the leg can still change hands; [] once it has
    /// a live request or has started. Nil from an older server — then the
    /// app offers both, as it always did.
    let actions: [String]?
    /// Every leg of a double, on `today` (C6). Nil for a single shift.
    let legs: [StaffShift]?
    /// `upcoming[]` carries the ISO date beside the CSV's own.
    let dateISO: String?

    enum CodingKeys: String, CodingKey {
        case date, day, role, notes, station, section, start, end, hours, request, actions, legs
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case scheduledHours = "scheduled_hours"
        case breakWindow = "break"
        case dateISO = "date_iso"
    }

    /// /staff/api/shifts sends `start`, `end` and `hours`
    /// (labor.employee_shifts_from_csv) and, since C6, `shift_start` /
    /// `shift_end` too; older payloads and the schedule views send
    /// `shift_start`, `shift_end`, `scheduled_hours`. All decode — reading
    /// only the second left every staff shift with no times (9/30/26).
    /// Every optional field is read leniently: one odd value never costs
    /// the employee their week.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = try? c.decodeIfPresent(String.self, forKey: .date)
        day = try? c.decodeIfPresent(String.self, forKey: .day)
        role = try? c.decodeIfPresent(String.self, forKey: .role)
        notes = try? c.decodeIfPresent(String.self, forKey: .notes)
        station = try? c.decodeIfPresent(String.self, forKey: .station)
        section = try? c.decodeIfPresent(String.self, forKey: .section)
        breakWindow = try? c.decodeIfPresent(String.self, forKey: .breakWindow)
        shiftStart = (try? c.decodeIfPresent(String.self, forKey: .shiftStart))
            ?? (try? c.decodeIfPresent(String.self, forKey: .start)) ?? nil
        shiftEnd = (try? c.decodeIfPresent(String.self, forKey: .shiftEnd))
            ?? (try? c.decodeIfPresent(String.self, forKey: .end)) ?? nil
        let numericHours = (try? c.decodeIfPresent(Double.self, forKey: .hours)) ?? nil
        if let h = try? c.decodeIfPresent(String.self, forKey: .scheduledHours) {
            scheduledHours = h
            hours = numericHours ?? Double(h)
        } else if let h = numericHours {
            scheduledHours = h == h.rounded() ? String(Int(h)) : String(h)
            hours = h
        } else {
            let text = (try? c.decodeIfPresent(String.self, forKey: .hours)) ?? nil
            scheduledHours = text
            hours = text.flatMap(Double.init)
        }
        request = (try? c.decodeIfPresent(StaffShiftRequestState.self, forKey: .request)) ?? nil
        actions = (try? c.decodeIfPresent([String].self, forKey: .actions)) ?? nil
        legs = (try? c.decodeIfPresent([StaffShift].self, forKey: .legs)) ?? nil
        dateISO = (try? c.decodeIfPresent(String.self, forKey: .dateISO)) ?? nil
    }

    /// Written back in the server's own spelling, so the device cache
    /// (StaffCache) decodes through the very init above.
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(date, forKey: .date)
        try c.encodeIfPresent(day, forKey: .day)
        try c.encodeIfPresent(role, forKey: .role)
        try c.encodeIfPresent(notes, forKey: .notes)
        try c.encodeIfPresent(station, forKey: .station)
        try c.encodeIfPresent(section, forKey: .section)
        try c.encodeIfPresent(breakWindow, forKey: .breakWindow)
        try c.encodeIfPresent(shiftStart, forKey: .shiftStart)
        try c.encodeIfPresent(shiftEnd, forKey: .shiftEnd)
        if let hours { try c.encode(hours, forKey: .hours) } else {
            try c.encodeIfPresent(scheduledHours, forKey: .scheduledHours)
        }
        try c.encodeIfPresent(request, forKey: .request)
        try c.encodeIfPresent(actions, forKey: .actions)
        try c.encodeIfPresent(legs, forKey: .legs)
        try c.encodeIfPresent(dateISO, forKey: .dateISO)
    }

    /// "4pm – 10pm" — the house time form (DESIGN_SYSTEM → Dates and
    /// times: "4pm" on the hour), whatever spelling the CSV used.
    var timeRange: String {
        let start = StaffTime.label(shiftStart ?? "")
        let end = StaffTime.label(shiftEnd ?? "")
        guard !start.isEmpty || !end.isEmpty else { return "" }
        return "\(start) – \(end)"
    }

    /// Every leg of this day: `legs` on a double, else this shift alone.
    var allLegs: [StaffShift] { (legs?.isEmpty == false) ? legs! : [self] }

    /// The leg's hours in words: "6 hrs", "6.5 hrs", "1 hr"; nil unknown.
    var hoursLabel: String? { StaffTime.hoursLabel(hours) }

    /// "Server · On Grill · Patio section · 6 hrs" — what the leg is, in
    /// one line, leaving out what isn't known.
    var detailLine: String {
        var parts: [String] = []
        if let role, !role.isEmpty { parts.append(role) }
        if let station, !station.isEmpty { parts.append("On \(station)") }
        if let section, !section.isEmpty { parts.append("\(section) section") }
        if let hoursLabel { parts.append(hoursLabel) }
        return parts.joined(separator: " \u{00B7} ")
    }

    /// "Break 6:30pm – 7pm", or nil when there is no meal window.
    var breakLine: String? {
        guard let window = breakWindow?.trimmingCharacters(in: .whitespaces), !window.isEmpty else { return nil }
        let ends = window.components(separatedBy: CharacterSet(charactersIn: "–-")).map {
            $0.trimmingCharacters(in: .whitespaces)
        }
        guard ends.count == 2 else { return "Break \(window)" }
        return "Break \(StaffTime.label(ends[0])) – \(StaffTime.label(ends[1]))"
    }

    /// The note the schedule carries, made staff-safe by the server; nil
    /// when blank.
    var noteLine: String? {
        guard let notes = notes?.trimmingCharacters(in: .whitespacesAndNewlines), !notes.isEmpty else { return nil }
        return notes
    }

    /// Whether this leg can still be given up / swapped: the server says so
    /// in `actions` (C6); an older server sent none, and the app offered
    /// both whenever the shift had a start time.
    var canDrop: Bool { shiftStart != nil && (actions ?? ["drop", "swap"]).contains("drop") }
    var canSwap: Bool { shiftStart != nil && (actions ?? ["drop", "swap"]).contains("swap") }
}

struct StaffWeekDay: Codable, Identifiable, Hashable {
    let date: String
    let weekday: String
    let isToday: Bool
    let off: Bool
    let shift: StaffShift?
    /// Every leg that day (C6); nil from an older server — read `legs`.
    var shifts: [StaffShift]? = nil
    /// Whether a published week covers the day at all. False is "Not posted
    /// yet", never "Off" (WF-19). Nil from an older server: posted.
    var posted: Bool? = nil

    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, weekday, off, shift, shifts, posted
        case isToday = "is_today"
    }

    /// Every leg, first to last — `shifts` when sent, else `shift`.
    var legs: [StaffShift] {
        if let shifts, !shifts.isEmpty { return shifts }
        return shift.map { [$0] } ?? []
    }

    var isPosted: Bool { posted ?? true }

    /// The same day narrowed to one leg — what the shift change sheet
    /// (StaffShiftChangeSheet(day:mode:)) acts on, so the evening half of
    /// a double can be given up on its own (C6).
    func focused(on leg: StaffShift) -> StaffWeekDay {
        StaffWeekDay(date: date, weekday: weekday, isToday: isToday, off: off, shift: leg,
                     shifts: [leg], posted: posted)
    }
}

struct StaffShiftsResponse: Codable {
    let ok: Bool
    /// False when the restaurant has never published a schedule. The portal
    /// says so plainly rather than showing anything invented — the backend
    /// deliberately does not fall back to the sample shift data the owner
    /// dashboard uses for previews.
    let published: Bool?
    let today: StaffShift?
    let week: [StaffWeekDay]?
    /// Every published shift from today on, past day 7 too (`date_iso`).
    var upcoming: [StaffShift]? = nil
    /// The scheduled hours over the seven days of `week` (H5).
    var weekHours: Double? = nil
    var weekStart: String? = nil
    var weekOf: String? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, published, today, week, upcoming, error
        case weekHours = "week_hours"
        case weekStart = "week_start"
        case weekOf = "week_of"
    }
}

struct StaffTask: Decodable, Identifiable, Hashable {
    let id: Int
    let label: String
    let done: Bool
    let completedBy: String?

    enum CodingKeys: String, CodingKey {
        case id, label, done
        case completedBy = "completed_by"
    }
}

struct StaffTasksResponse: Decodable {
    let ok: Bool
    /// The employee's JOB role ("Bartender"), not their authorization role.
    let role: String?
    let tasks: [StaffTask]?
    /// Today's sheets that are this person's (task_sheets.staff_view), and
    /// for a manager the floor and the shifts they may sign off.
    let sheets: [StaffSheet]?
    let manager: Bool?
    let floor: [StaffSheet]?
    let canSignOff: [String]?
    let signoffs: [StaffSignoff]?
    /// A refusal's sentence, when the server answered 200 with ok:false.
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, role, tasks, sheets, manager, floor, signoffs, error
        case canSignOff = "can_sign_off"
    }
}

struct StaffProfile: Codable, Hashable {
    let name: String
    let role: String?
    let restaurant: String
    let hasPin: Bool

    enum CodingKeys: String, CodingKey {
        case name, role, restaurant
        case hasPin = "has_pin"
    }
}

struct StaffProfileResponse: Decodable {
    let ok: Bool
    let employee: StaffProfile?
    var error: String? = nil
}

struct StaffOKResponse: Decodable {
    let ok: Bool
    let error: String?
}

// MARK: - Self-signup
//
// An employee makes their own account: verify a phone, name the restaurant
// with a join code, claim their own name off the roster, set a PIN. The owner
// does nothing. The control is on which NAME may be claimed — once, from the
// real roster — not on who may sign up, because an account with no membership
// can see nothing at all.

struct StaffSignupStartResponse: Decodable {
    let ok: Bool
    let error: String?
    let smsSent: Bool?
    /// Present only when Twilio is unconfigured off a deployed environment, so
    /// the flow is testable locally. The server decides; it can never appear
    /// in production.
    let devCode: String?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case smsSent = "sms_sent"
        case devCode = "dev_code"
    }
}

struct StaffSignupVerifyResponse: Decodable {
    let ok: Bool
    let error: String?
    let signupToken: String?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case signupToken = "signup_token"
    }
}

struct StaffRestaurantLookup: Decodable {
    let ok: Bool
    let error: String?
    let restaurant: String?
}

struct StaffClaimableName: Decodable, Identifiable, Hashable {
    let name: String
    let jobRole: String?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case jobRole = "job_role"
    }
}

struct StaffClaimableResponse: Decodable {
    let ok: Bool
    let error: String?
    let restaurant: String?
    let names: [StaffClaimableName]?
    /// True when every name on the roster is already taken — a real state
    /// with its own answer ("ask your manager"), not an empty list.
    let noneLeft: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, error, restaurant, names
        case noneLeft = "none_left"
    }
}

struct StaffClaimResponse: Decodable {
    let ok: Bool
    let error: String?
    let token: String?
    let employeeName: String?
    let jobRole: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, token
        case employeeName = "employee_name"
        case jobRole = "job_role"
    }
}

// MARK: - Today (employee audit V1, wave 2 I2)
//
// The Today screen's own payloads, read from the routes the backend fixes
// (B3/B5/B6/B7) added. Every optional field decodes leniently, and every
// payload that can say "no" carries `error`. Names are Today's own
// (StaffCoworker, StaffWaitingItem, StaffInboxBadge…), so the Requests and
// Inbox screens keep their own fuller models without a clash.

/// "Who's on with me" — GET /staff/api/colleagues?date= (H7).
struct StaffCoworker: Codable, Hashable, Identifiable {
    let name: String
    let role: String?
    let station: String?
    let shiftStart: String?
    let shiftEnd: String?

    var id: String { name + "|" + (shiftStart ?? "") }

    enum CodingKeys: String, CodingKey {
        case name, role, station
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
    }

    var timeRange: String {
        let s = StaffTime.label(shiftStart ?? ""), e = StaffTime.label(shiftEnd ?? "")
        guard !s.isEmpty || !e.isEmpty else { return "" }
        return "\(s) – \(e)"
    }
}

struct StaffCoworkersResponse: Codable {
    let ok: Bool
    var date: String? = nil
    /// Whether a published week covers `date` at all.
    var posted: Bool? = nil
    var coworkers: [StaffCoworker]? = nil
    var note: String? = nil
    var error: String? = nil
}

/// One "running late" report — POST/GET /staff/api/running-late (H1).
struct StaffRunningLateReport: Codable, Hashable, Identifiable {
    let id: Int
    let date: String
    let shiftStart: String
    let role: String?
    let etaMinutes: Int
    let note: String?
    let reportedAt: String?
    let updatedAt: String?
    let managersTold: Bool?

    enum CodingKeys: String, CodingKey {
        case id, date, role, note
        case shiftStart = "shift_start"
        case etaMinutes = "eta_minutes"
        case reportedAt = "reported_at"
        case updatedAt = "updated_at"
        case managersTold = "managers_told"
    }
}

struct StaffRunningLateList: Codable {
    let ok: Bool
    var reports: [StaffRunningLateReport]? = nil
    var etaChoices: [Int]? = nil
    var etaMax: Int? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, reports, error
        case etaChoices = "eta_choices"
        case etaMax = "eta_max"
    }
}

struct StaffRunningLateBody: Encodable, Equatable {
    let date: String
    let shiftStart: String
    let etaMinutes: Int
    let note: String?

    enum CodingKeys: String, CodingKey {
        case date, note
        case shiftStart = "shift_start"
        case etaMinutes = "eta_minutes"
    }
}

struct StaffRunningLateResult: Decodable {
    let ok: Bool
    var created: Bool? = nil
    var managersTold: Bool? = nil
    var report: StaffRunningLateReport? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, created, report, error
        case managersTold = "managers_told"
    }
}

/// The inbox's two counts — GET /staff/api/inbox (B5). Only the counts:
/// the Inbox screen decodes the announcements itself.
struct StaffInboxBadge: Codable {
    let ok: Bool
    var unread: Int? = nil
    var unreadMessages: Int? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, unread, error
        case unreadMessages = "unread_messages"
    }

    var total: Int { max(0, unread ?? 0) + max(0, unreadMessages ?? 0) }
}

/// A swap asked of this person, or a shift offered to them by name — what
/// "Waiting on you" counts (GET /staff/api/shift-requests `asks`, `offers`).
struct StaffWaitingItem: Codable, Hashable, Identifiable {
    let id: Int
    let date: String?
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?
    let employeeName: String?
    let targetDate: String?
    let targetStart: String?

    enum CodingKeys: String, CodingKey {
        case id, date, role
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case employeeName = "employee_name"
        case targetDate = "target_date"
        case targetStart = "target_start"
    }
}

struct StaffWaitingResponse: Codable {
    let ok: Bool
    var asks: [StaffWaitingItem]? = nil
    var offers: [StaffWaitingItem]? = nil
    var error: String? = nil

    /// The Requests tab's red count: swaps asked of me + shifts offered me.
    var count: Int { (asks?.count ?? 0) + (offers?.count ?? 0) }
}

/// GET /staff/api/preshift, made personal (staff_brief.personal, B7).
struct StaffBriefItem: Codable, Hashable {
    let kind: String
    let text: String
}

struct StaffBriefFocus: Codable, Hashable {
    let item: String
    let line: String?
}

struct StaffPersonalBrief: Codable {
    let ok: Bool
    var day: String? = nil
    var weekday: String? = nil
    /// true: on today; false: off today (nothing to read); nil: no
    /// published schedule, so the server can't tell — the card shows.
    var working: Bool? = nil
    var published: Bool? = nil
    var you: [StaffBriefItem]? = nil
    var items: [StaffBriefItem]? = nil
    /// Manager-approved text only.
    var briefText: String? = nil
    var focus: StaffBriefFocus? = nil
    var message: String? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, day, weekday, working, published, you, items, focus, message, error
        case briefText = "brief_text"
    }

    /// The day's items without the reader's own role/hours/station lines —
    /// the hero card already says those.
    var dayItems: [StaffBriefItem] {
        (items ?? []).filter { $0.kind != "you" && $0.kind != "station" }
    }

    /// Shown only on a working day (UX-36): never when the server says
    /// they're off, and never when there's nothing to read.
    var shouldShow: Bool {
        guard working != false else { return false }
        return !dayItems.isEmpty || !(briefText ?? "").isEmpty || focus != nil
    }
}

/// GET /staff/api/stats (B6 V2): the caller's own hours, never money.
struct StaffStats: Codable {
    struct Week: Codable {
        let start: String?
        let end: String?
        let label: String?
    }

    struct Scheduled: Codable {
        let hours: Double?
        let shifts: Int?
    }

    struct Actual: Codable {
        let available: Bool?
        let hours: Double?
        let asOf: String?
        let asOfLabel: String?
        let note: String?

        enum CodingKeys: String, CodingKey {
            case available, hours, note
            case asOf = "as_of"
            case asOfLabel = "as_of_label"
        }
    }

    struct Overtime: Codable {
        let applies: Bool?
        let lineHours: Double?
        let projectedHours: Double?
        let headroomHours: Double?
        let over: Bool?
        let message: String?

        enum CodingKeys: String, CodingKey {
            case applies, over, message
            case lineHours = "line_hours"
            case projectedHours = "projected_hours"
            case headroomHours = "headroom_hours"
        }
    }

    let ok: Bool
    var today: String? = nil
    var week: Week? = nil
    var scheduled: Scheduled? = nil
    var actual: Actual? = nil
    var overtime: Overtime? = nil
    var error: String? = nil

    /// The overtime heads-up in hours, only when it applies and says
    /// something ("A pickup longer than 5.5h takes you past 40 hours…").
    var overtimeLine: String? {
        guard overtime?.applies == true, let m = overtime?.message, !m.isEmpty else { return nil }
        return m
    }
}

/// GET /staff/api/earnings (B6 H11): the caller's own punches and tips as
/// the POS reports them, a day behind. Never pay.
struct StaffEarnings: Codable {
    struct Shift: Codable, Hashable {
        let businessDate: String
        let dateLabel: String?
        let weekday: String?
        let role: String?
        let hours: Double?
        let tipsTotal: Double?
        let stillOpen: Bool?

        enum CodingKeys: String, CodingKey {
            case weekday, role, hours
            case businessDate = "business_date"
            case dateLabel = "date_label"
            case tipsTotal = "tips_total"
            case stillOpen = "still_open"
        }
    }

    let ok: Bool
    var available: Bool? = nil
    var reason: String? = nil
    var message: String? = nil
    var asOf: String? = nil
    var asOfLabel: String? = nil
    var lagNote: String? = nil
    var tipsNote: String? = nil
    var shifts: [Shift]? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, available, reason, message, shifts, error
        case asOf = "as_of"
        case asOfLabel = "as_of_label"
        case lagNote = "lag_note"
        case tipsNote = "tips_note"
    }

    /// The newest finished shift with its tips — what Today's tile shows.
    /// Nil when the POS isn't connected or nothing is in yet (never $0
    /// standing in for "unknown").
    var lastShift: Shift? {
        guard available == true else { return nil }
        return (shifts ?? []).first { $0.stillOpen != true && $0.tipsTotal != nil }
    }
}

/// GET /staff/api/recognition (B6 V5): guests who named this person.
struct StaffRecognition: Codable {
    struct Item: Codable, Hashable, Identifiable {
        let id: Int
        let date: String?
        let dateLabel: String?
        let excerpt: String?

        enum CodingKeys: String, CodingKey {
            case id, date, excerpt
            case dateLabel = "date_label"
        }
    }

    let ok: Bool
    var items: [Item]? = nil
    var count: Int? = nil
    var error: String? = nil
}

/// The payloads Today reads that can answer 200 with `ok:false` — a
/// refusal is a failed load with the server's sentence, never "empty".
protocol StaffRefusable {
    var ok: Bool { get }
    var error: String? { get }
}

extension StaffShiftsResponse: StaffRefusable {}
extension StaffTasksResponse: StaffRefusable {}
extension StaffProfileResponse: StaffRefusable {}
extension StaffCoworkersResponse: StaffRefusable {}
extension StaffRunningLateList: StaffRefusable {}
extension StaffInboxBadge: StaffRefusable {}
extension StaffWaitingResponse: StaffRefusable {}
extension StaffPersonalBrief: StaffRefusable {}
extension StaffStats: StaffRefusable {}
extension StaffEarnings: StaffRefusable {}
extension StaffRecognition: StaffRefusable {}
