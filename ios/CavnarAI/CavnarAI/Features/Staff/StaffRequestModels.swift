import Foundation

// The employee app's request, inbox and Me payloads (employee audit wave 2,
// I3). Each type mirrors one /staff/api route as the server on this branch
// answers it — staff_routes.py, staff_comms_routes.py, staff_me_routes.py,
// staff_knowledge_routes.py, staff_device_routes.py — and decodes
// tolerantly: a key the server leaves out reads as its empty form, never as
// a failure, while a body that is not the route's shape at all still throws,
// so a screen can tell "nothing here" from "that didn't load" (UX-07, C7).
//
// StaffModels.swift (shifts, tasks, sign-in) belongs to the container; the
// types here are this file's own, even where they overlap a field there.

// MARK: - Small decoding helpers

extension KeyedDecodingContainer {
    /// A string, or nil when absent, null or another type.
    func staffString(_ key: Key) -> String? {
        if let s = try? decodeIfPresent(String.self, forKey: key) { return s }
        if let i = try? decodeIfPresent(Int.self, forKey: key) { return String(i) }
        return nil
    }

    func staffBool(_ key: Key, default fallback: Bool = false) -> Bool {
        if let b = try? decodeIfPresent(Bool.self, forKey: key) { return b }
        if let i = try? decodeIfPresent(Int.self, forKey: key) { return i != 0 }
        return fallback
    }

    func staffInt(_ key: Key) -> Int? {
        if let i = try? decodeIfPresent(Int.self, forKey: key) { return i }
        if let d = try? decodeIfPresent(Double.self, forKey: key) { return Int(d) }
        if let s = try? decodeIfPresent(String.self, forKey: key) { return Int(s) }
        return nil
    }

    func staffArray<T: Decodable>(_ key: Key, of type: T.Type = T.self) -> [T] {
        (try? decodeIfPresent([T].self, forKey: key)) ?? []
    }
}

// MARK: - Times and dates as an employee reads them

/// Times on the staff screens read like the rest of the app: "4pm", "4:30pm"
/// (DESIGN_SYSTEM → Dates and times; UX-35). The server sends shift times
/// as the schedule wrote them ("4:00pm") and availability as "17:00".
enum StaffClock {
    /// Minutes after midnight for "17:00", "5:00pm", "5pm" or "5:30 PM";
    /// nil for anything else.
    static func minutes(_ raw: String?) -> Int? {
        guard var s = raw?.trimmingCharacters(in: .whitespaces).lowercased(), !s.isEmpty else { return nil }
        var meridiem: String?
        for m in ["am", "pm", "a", "p"] where s.hasSuffix(m) {
            meridiem = m.hasPrefix("a") ? "am" : "pm"
            s = String(s.dropLast(m.count)).trimmingCharacters(in: .whitespaces)
            break
        }
        let parts = s.split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 1 || parts.count == 2, let h = Int(parts[0]) else { return nil }
        let m = parts.count == 2 ? Int(parts[1]) : 0
        guard let m, (0..<60).contains(m) else { return nil }
        if let meridiem {
            guard (1...12).contains(h) else { return nil }
            let h24 = (h % 12) + (meridiem == "pm" ? 12 : 0)
            return h24 * 60 + m
        }
        guard (0..<24).contains(h) else { return nil }
        return h * 60 + m
    }

    /// "17:00" for 1020.
    static func hhmm(_ minutes: Int) -> String {
        let m = ((minutes % 1440) + 1440) % 1440
        return String(format: "%02d:%02d", m / 60, m % 60)
    }

    /// "5pm" / "5:30pm" — the raw string back when it can't be read.
    static func display(_ raw: String?) -> String {
        guard let raw else { return "" }
        guard let mins = minutes(raw) else { return raw }
        let h = mins / 60, m = mins % 60
        let h12 = h % 12 == 0 ? 12 : h % 12
        let suffix = h < 12 ? "am" : "pm"
        return m == 0 ? "\(h12)\(suffix)" : "\(h12):\(String(format: "%02d", m))\(suffix)"
    }

    /// "4pm – 10pm", "4pm", or "".
    static func range(_ start: String?, _ end: String?) -> String {
        let s = display(start), e = display(end)
        if s.isEmpty { return e }
        return e.isEmpty ? s : "\(s) – \(e)"
    }

    /// The choices a window offers: every half hour, starting at 5am so a
    /// closer's "done by 2am" sits at the end of the list, not the top.
    static let choices: [String] = (0..<48).map { hhmm(5 * 60 + $0 * 30) }
}

enum StaffDay {
    private static let utc: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(secondsFromGMT: 0)!
        return c
    }()

    /// The calendar day an ISO date names, read in UTC so the phone's own
    /// zone can never move it a day.
    static func date(_ iso: String?) -> Date? {
        guard let iso else { return nil }
        let p = iso.prefix(10).split(separator: "-")
        guard p.count == 3, let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]) else { return nil }
        return utc.date(from: DateComponents(year: y, month: m, day: d))
    }

    /// "Sat" for "2026-09-26"; "" when unreadable.
    static func shortWeekday(_ iso: String?) -> String {
        guard let d = date(iso) else { return "" }
        let i = utc.component(.weekday, from: d)       // 1 = Sunday
        return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][(i - 1) % 7]
    }

    /// "Sat 9/26/26 · 4pm – 10pm" — the one way a shift is named on these
    /// screens, so the confirm, the row and the thread's context agree.
    static func shift(_ iso: String?, _ start: String?, _ end: String? = nil) -> String {
        let day = [shortWeekday(iso), iso.map(CavnarDate.mdy) ?? ""].filter { !$0.isEmpty }.joined(separator: " ")
        let time = StaffClock.range(start, end)
        return [day, time].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// `yyyy-MM-dd` for a picked Date on the phone's calendar.
    static func iso(_ date: Date) -> String { CavnarDate.isoDay(date) }
}

// MARK: - Bodies (the exact keys the routes read)

/// {accept: true|false} — the offer route refuses anything but a JSON bool.
struct StaffAcceptBody: Encodable, Equatable {
    let accept: Bool
}

struct StaffEmptyBody: Encodable {}

// MARK: - Requests board (GET /staff/api/shift-requests)

/// A status chip's words and tone.
struct StaffStatusChip: Equatable {
    let text: String
    let tone: CavnarTone
}

/// One of my own shift requests: a drop ("Giving up"), a swap, or a shift a
/// manager posted open for me (kind "post", which only the manager takes down).
struct StaffMyShiftRequest: Decodable, Identifiable, Hashable {
    let id: Int
    let kind: String
    let date: String
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?
    let status: String
    let reason: String?
    let targetName: String?
    let targetDate: String?
    let targetStart: String?
    let targetEnd: String?
    let targetAccepted: Bool
    let replacementName: String?
    let decisionNote: String?
    let decidedAt: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, date, role, status, reason
        case shiftStart = "shift_start", shiftEnd = "shift_end"
        case targetName = "target_name", targetDate = "target_date"
        case targetStart = "target_start", targetEnd = "target_end"
        case targetAccepted = "target_accepted", replacementName = "replacement_name"
        case decisionNote = "decision_note", decidedAt = "decided_at", createdAt = "created_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        kind = c.staffString(.kind) ?? "drop"
        date = c.staffString(.date) ?? ""
        shiftStart = c.staffString(.shiftStart)
        shiftEnd = c.staffString(.shiftEnd)
        role = c.staffString(.role)
        status = c.staffString(.status) ?? "pending"
        reason = c.staffString(.reason)
        targetName = c.staffString(.targetName)
        targetDate = c.staffString(.targetDate)
        targetStart = c.staffString(.targetStart)
        targetEnd = c.staffString(.targetEnd)
        targetAccepted = c.staffBool(.targetAccepted)
        replacementName = c.staffString(.replacementName)
        decisionNote = c.staffString(.decisionNote)
        decidedAt = c.staffString(.decidedAt)
        createdAt = c.staffString(.createdAt)
    }

    var isSwap: Bool { kind == "swap" }
    var isLive: Bool { ["pending", "approved", "open"].contains(status) }

    /// One verb pair everywhere (UX-34): "Swap" / "Giving up".
    var tag: String {
        switch kind {
        case "swap": return "Swap"
        case "post": return "Posted open"
        default: return "Giving up"
        }
    }

    /// Withdraw applies to my own live drops and swaps; a shift my manager
    /// posted open is theirs to take down (shift_requests.withdraw).
    var canWithdraw: Bool { isLive && kind != "post" }

    var shiftLabel: String { StaffDay.shift(date, shiftStart, shiftEnd) }

    var targetLabel: String? {
        guard isSwap, targetDate != nil || targetStart != nil else { return nil }
        return StaffDay.shift(targetDate, targetStart, targetEnd)
    }

    var chip: StaffStatusChip {
        switch status {
        case "pending":
            if isSwap, let who = targetName, !who.isEmpty {
                return StaffStatusChip(text: targetAccepted ? "\(who) said yes" : "Waiting", tone: .neutral)
            }
            return StaffStatusChip(text: "Waiting", tone: .neutral)
        case "approved":
            return StaffStatusChip(text: "Approved", tone: .neutral)
        case "open":
            return StaffStatusChip(text: "Open — you're still on it", tone: .warning)
        case "covered":
            return StaffStatusChip(text: isSwap ? "Swapped" : "Covered", tone: .good)
        case "denied":
            return StaffStatusChip(text: "Not approved", tone: .bad)
        case "withdrawn":
            return StaffStatusChip(text: "Withdrawn", tone: .neutral)
        case "cancelled":
            return StaffStatusChip(text: "Cancelled", tone: .neutral)
        case "expired":
            return StaffStatusChip(text: "Expired", tone: .neutral)
        default:
            return StaffStatusChip(text: status.capitalized, tone: .neutral)
        }
    }

    /// The sentence under the chip: who it waits on, or what happened.
    var detail: String? {
        let who = (targetName ?? "").isEmpty ? "your colleague" : targetName!
        switch status {
        case "pending":
            if isSwap {
                return targetAccepted ? "\(who) said yes — waiting on your manager."
                                      : "Waiting on your manager and \(who)."
            }
            return "Waiting on your manager."
        case "approved":
            return isSwap ? "Your manager said yes — waiting on \(who)." : "Your manager said yes."
        case "open":
            return "Your manager approved it. You're still on this shift until someone picks it up — we'll tell you when they do."
        case "covered":
            if isSwap { return "The swap went through." }
            return replacementName.map { "\($0) took it. It's off your schedule." } ?? "Someone took it. It's off your schedule."
        case "expired":
            return "The shift passed before anyone answered."
        default:
            return nil
        }
    }
}

/// A swap a colleague asked of me: their shift (date/shift_start) for mine
/// (target_*). Never carries their reason.
struct StaffSwapAsk: Decodable, Identifiable, Hashable {
    let id: Int
    let date: String
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?
    let status: String
    let employeeName: String
    let targetDate: String?
    let targetStart: String?
    let targetEnd: String?
    let managerApproved: Bool

    enum CodingKeys: String, CodingKey {
        case id, date, role, status
        case shiftStart = "shift_start", shiftEnd = "shift_end", employeeName = "employee_name"
        case targetDate = "target_date", targetStart = "target_start", targetEnd = "target_end"
        case managerApproved = "manager_approved"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        date = c.staffString(.date) ?? ""
        shiftStart = c.staffString(.shiftStart)
        shiftEnd = c.staffString(.shiftEnd)
        role = c.staffString(.role)
        status = c.staffString(.status) ?? "pending"
        employeeName = c.staffString(.employeeName) ?? "A colleague"
        targetDate = c.staffString(.targetDate)
        targetStart = c.staffString(.targetStart)
        targetEnd = c.staffString(.targetEnd)
        managerApproved = c.staffBool(.managerApproved)
    }

    var theirShift: String { StaffDay.shift(date, shiftStart, shiftEnd) }
    var yourShift: String { StaffDay.shift(targetDate, targetStart, targetEnd) }

    /// The Accept confirm names what moves both ways (UX-14).
    var confirmMessage: String {
        let after = managerApproved ? " Your manager already said yes, so it happens as soon as you accept."
                                    : " It happens once your manager approves too."
        return "You'll work \(employeeName)'s \(theirShift); \(employeeName) takes your \(yourShift).\(after)"
    }
}

/// A shift a manager offered to me by name (H2).
struct StaffShiftOffer: Decodable, Identifiable, Hashable {
    let id: Int
    let requestId: Int?
    let status: String
    let date: String
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?
    let employeeName: String?
    let note: String?
    let forCoverage: Bool
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, status, date, role, note
        case requestId = "request_id", shiftStart = "shift_start", shiftEnd = "shift_end"
        case employeeName = "employee_name", forCoverage = "for_coverage", createdAt = "created_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        requestId = c.staffInt(.requestId)
        status = c.staffString(.status) ?? "offered"
        date = c.staffString(.date) ?? ""
        shiftStart = c.staffString(.shiftStart)
        shiftEnd = c.staffString(.shiftEnd)
        role = c.staffString(.role)
        let who = c.staffString(.employeeName)?.trimmingCharacters(in: .whitespaces)
        employeeName = (who?.isEmpty ?? true) ? nil : who
        let n = c.staffString(.note)?.trimmingCharacters(in: .whitespacesAndNewlines)
        note = (n?.isEmpty ?? true) ? nil : n
        forCoverage = c.staffBool(.forCoverage)
        createdAt = c.staffString(.createdAt)
    }

    var shiftLabel: String { StaffDay.shift(date, shiftStart, shiftEnd) }

    /// "Your manager offered you Ana's shift" / "…an extra shift".
    var headline: String {
        if let employeeName { return "Your manager offered you \(employeeName)'s shift" }
        return "Your manager offered you an extra shift"
    }
}

/// An open shift on the board. `canTake` is nil past the server's judging
/// budget — the claim itself still checks.
struct StaffOpenShift: Decodable, Identifiable, Hashable {
    let id: Int
    let kind: String
    let date: String
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?
    let employeeName: String?
    let postedByManager: Bool
    let canTake: Bool?
    let whyNot: String?
    /// "This 7h pickup takes you past 40 hours that week" — staff_insights.
    /// pickup_overtime_note, when the board carries it (not wired on the
    /// server yet; shown as soon as it is).
    let overtimeNote: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, date, role
        case shiftStart = "shift_start", shiftEnd = "shift_end", employeeName = "employee_name"
        case postedByManager = "posted_by_manager", canTake = "can_take", whyNot = "why_not"
        case overtimeNote = "overtime_note"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        kind = c.staffString(.kind) ?? "drop"
        date = c.staffString(.date) ?? ""
        shiftStart = c.staffString(.shiftStart)
        shiftEnd = c.staffString(.shiftEnd)
        role = c.staffString(.role)
        let who = c.staffString(.employeeName)?.trimmingCharacters(in: .whitespaces)
        employeeName = (who?.isEmpty ?? true) ? nil : who
        postedByManager = c.staffBool(.postedByManager)
        canTake = try? c.decodeIfPresent(Bool.self, forKey: .canTake)
        whyNot = c.staffString(.whyNot)
        overtimeNote = c.staffString(.overtimeNote)
    }

    var shiftLabel: String { StaffDay.shift(date, shiftStart, shiftEnd) }
    var takeable: Bool { canTake != false }

    /// The reason it can't be mine, as a sentence ("You can't take this one:
    /// …") — the server's words, capitalised.
    var whyNotLine: String? {
        guard canTake == false else { return nil }
        guard let w = whyNot, !w.isEmpty else { return "You can't take this one." }
        return "You can't take this one: " + w.prefix(1).lowercased() + w.dropFirst()
    }
}

struct StaffRequestsBoard: Decodable, Equatable {
    let requests: [StaffMyShiftRequest]
    let asks: [StaffSwapAsk]
    let offers: [StaffShiftOffer]
    let open: [StaffOpenShift]

    enum CodingKeys: String, CodingKey { case ok, error, requests, asks, offers, open }

    init(requests: [StaffMyShiftRequest] = [], asks: [StaffSwapAsk] = [],
         offers: [StaffShiftOffer] = [], open: [StaffOpenShift] = []) {
        self.requests = requests; self.asks = asks; self.offers = offers; self.open = open
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // `{ok:false}` is a refusal, not an empty board.
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your shift requests.")
        }
        requests = c.staffArray(.requests)
        asks = c.staffArray(.asks)
        offers = c.staffArray(.offers).filter { $0.status == "offered" }
        open = c.staffArray(.open)
    }

    /// What waits on this person's answer: swaps asked of them and shifts
    /// offered to them by name. Open shifts are a chance, not a wait, so
    /// they don't count toward the Requests badge.
    var waitingCount: Int { asks.count + offers.count }
    var hasWaiting: Bool { !asks.isEmpty || !offers.isEmpty || !open.isEmpty }
}

// MARK: - Time off (GET /staff/api/time-off)

struct StaffScheduledShift: Decodable, Hashable {
    let date: String
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?

    enum CodingKeys: String, CodingKey {
        case date, role
        case shiftStart = "shift_start", shiftEnd = "shift_end"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = c.staffString(.date) ?? ""
        shiftStart = c.staffString(.shiftStart)
        shiftEnd = c.staffString(.shiftEnd)
        role = c.staffString(.role)
    }

    var label: String { StaffDay.shift(date, shiftStart, shiftEnd) }
}

struct StaffTimeOffRequest: Decodable, Identifiable, Hashable {
    let id: Int
    let startDate: String
    let endDate: String
    let reason: String?
    let status: String
    let decisionNote: String?
    /// An approved range that hasn't started: called off directly (M6).
    let canCancel: Bool
    /// Published shifts still inside an approved range — the manager has
    /// to move them; the employee should know they're still on them.
    let stillScheduled: [StaffScheduledShift]

    enum CodingKeys: String, CodingKey {
        case id, reason, status
        case startDate = "start_date", endDate = "end_date", decisionNote = "decision_note"
        case canCancel = "can_cancel", stillScheduled = "still_scheduled"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        startDate = c.staffString(.startDate) ?? ""
        endDate = c.staffString(.endDate) ?? ""
        reason = c.staffString(.reason)
        status = c.staffString(.status) ?? "pending"
        decisionNote = c.staffString(.decisionNote)
        canCancel = c.staffBool(.canCancel)
        stillScheduled = c.staffArray(.stillScheduled)
    }

    var isLive: Bool { status == "pending" || status == "approved" }
    var rangeLabel: String { CavnarDate.mdyRange(startDate, endDate) }

    var chip: StaffStatusChip {
        switch status {
        case "approved": return StaffStatusChip(text: "Approved", tone: .good)
        case "denied": return StaffStatusChip(text: "Not approved", tone: .bad)
        case "withdrawn": return StaffStatusChip(text: "Withdrawn", tone: .neutral)
        default: return StaffStatusChip(text: "Waiting", tone: .neutral)
        }
    }
}

struct StaffTimeOffList: Decodable, Equatable {
    let requests: [StaffTimeOffRequest]

    enum CodingKeys: String, CodingKey { case ok, error, requests }

    init(requests: [StaffTimeOffRequest]) { self.requests = requests }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your time off.")
        }
        requests = c.staffArray(.requests)
    }
}

/// "Your requests": time off and shift changes in one list, the live ones
/// first (soonest first), then the answered ones (most recent first).
enum StaffYourRequest: Identifiable, Hashable {
    case timeOff(StaffTimeOffRequest)
    case shift(StaffMyShiftRequest)

    var id: String {
        switch self {
        case .timeOff(let t): return "to\(t.id)"
        case .shift(let s): return "sr\(s.id)"
        }
    }

    var isLive: Bool {
        switch self {
        case .timeOff(let t): return t.isLive
        case .shift(let s): return s.isLive
        }
    }

    var date: String {
        switch self {
        case .timeOff(let t): return t.startDate
        case .shift(let s): return s.date
        }
    }

    static func merged(timeOff: [StaffTimeOffRequest], shifts: [StaffMyShiftRequest],
                       hiding hidden: Set<String> = []) -> [StaffYourRequest] {
        let all = timeOff.map(StaffYourRequest.timeOff) + shifts.map(StaffYourRequest.shift)
        let shown = all.filter { !hidden.contains($0.id) }
        let live = shown.filter(\.isLive).sorted { ($0.date, $0.id) < ($1.date, $1.id) }
        let done = shown.filter { !$0.isLive }.sorted { ($0.date, $0.id) > ($1.date, $1.id) }
        return live + done
    }
}

// MARK: - Colleagues (GET /staff/api/colleagues?shift_date=&shift_start=)

struct StaffSwapCandidate: Decodable, Identifiable, Hashable {
    struct Shift: Decodable, Identifiable, Hashable {
        let date: String
        let role: String?
        let shiftStart: String
        let shiftEnd: String?
        /// False past the server's 2.5s judging budget: not yet checked,
        /// still checked when the swap is asked.
        let checked: Bool

        var id: String { date + "|" + shiftStart }
        var label: String { StaffDay.shift(date, shiftStart, shiftEnd) }

        enum CodingKeys: String, CodingKey {
            case date, role, checked
            case shiftStart = "shift_start", shiftEnd = "shift_end"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            date = c.staffString(.date) ?? ""
            role = c.staffString(.role)
            shiftStart = c.staffString(.shiftStart) ?? ""
            shiftEnd = c.staffString(.shiftEnd)
            checked = c.staffBool(.checked, default: true)
        }
    }

    let name: String
    let shifts: [Shift]
    var id: String { name }

    enum CodingKeys: String, CodingKey { case name, shifts }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.staffString(.name) ?? ""
        shifts = c.staffArray(.shifts).filter { !$0.shiftStart.isEmpty }
    }
}

struct StaffSwapCandidates: Decodable, Equatable {
    let colleagues: [StaffSwapCandidate]
    let note: String?

    enum CodingKeys: String, CodingKey { case ok, error, colleagues, note }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load who can swap.")
        }
        colleagues = c.staffArray(.colleagues, of: StaffSwapCandidate.self).filter { !$0.shifts.isEmpty }
        note = c.staffString(.note)
    }
}

// MARK: - Availability (GET/POST /staff/api/availability, M5)

struct StaffHint: Decodable, Hashable {
    let kind: String
    let text: String
}

/// One weekday: any time, off, or a window ("no start before 17:00",
/// "done by 23:00"), either optionally bounded by dates.
struct StaffAvailabilityDay: Codable, Identifiable, Hashable {
    enum Status: String, Codable, CaseIterable, Hashable {
        case any, off, window

        var label: String {
            switch self {
            case .any: return "Any time"
            case .off: return "Can't work"
            case .window: return "Some hours"
            }
        }
    }

    var day: String
    var status: Status
    var earliest: String?
    var latest: String?
    var from: String?
    var until: String?

    var id: String { day }

    enum CodingKeys: String, CodingKey { case day, status, earliest, latest, from, until }

    init(day: String, status: Status = .any, earliest: String? = nil, latest: String? = nil,
         from: String? = nil, until: String? = nil) {
        self.day = day; self.status = status; self.earliest = earliest; self.latest = latest
        self.from = from; self.until = until
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = c.staffString(.day) ?? ""
        status = Status(rawValue: (c.staffString(.status) ?? "any").lowercased()) ?? .any
        earliest = c.staffString(.earliest).flatMap { $0.isEmpty ? nil : $0 }
        latest = c.staffString(.latest).flatMap { $0.isEmpty ? nil : $0 }
        from = c.staffString(.from).flatMap { $0.isEmpty ? nil : $0 }
        until = c.staffString(.until).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// Every key, nulls included: the server reads the whole week.
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(day, forKey: .day)
        try c.encode(status.rawValue, forKey: .status)
        let window = status == .window
        try c.encode(window ? earliest : nil, forKey: .earliest)
        try c.encode(window ? latest : nil, forKey: .latest)
        let bounded = status != .any
        try c.encode(bounded ? from : nil, forKey: .from)
        try c.encode(bounded ? until : nil, forKey: .until)
    }

    /// "Any time", "Can't work", "Not before 5pm", "Done by 11pm",
    /// "5pm – 11pm", with " · until 12/15/26" / " · from 10/10/26".
    var summary: String {
        var text: String
        switch status {
        case .any: text = "Any time"
        case .off: text = "Can't work"
        case .window:
            switch (earliest, latest) {
            case let (e?, l?): text = "\(StaffClock.display(e)) – \(StaffClock.display(l))"
            case let (e?, nil): text = "Not before \(StaffClock.display(e))"
            case let (nil, l?): text = "Done by \(StaffClock.display(l))"
            default: text = "Some hours"
            }
        }
        guard status != .any else { return text }
        if let f = from { text += " · from \(CavnarDate.mdy(f))" }
        if let u = until { text += " · until \(CavnarDate.mdy(u))" }
        return text
    }
}

struct StaffAvailabilityRecord: Decodable, Hashable {
    static let weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    let week: [StaffAvailabilityDay]
    let unavailableDays: [String]
    let notes: String
    /// The version a save must send back, exactly as given; nil when the
    /// person has no record yet (sent as JSON null).
    let updatedAt: String?
    let timeOffHint: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, week, notes
        case unavailableDays = "unavailable_days", updatedAt = "updated_at", timeOffHint = "time_off_hint"
    }

    init(week: [StaffAvailabilityDay], unavailableDays: [String] = [], notes: String = "",
         updatedAt: String? = nil, timeOffHint: String? = nil) {
        self.week = week; self.unavailableDays = unavailableDays; self.notes = notes
        self.updatedAt = updatedAt; self.timeOffHint = timeOffHint
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your availability.")
        }
        // Without `week` this isn't the M5 answer at all; an editable form
        // built from nothing is the failure UX-07 described.
        guard c.contains(.week) else {
            throw APIClient.APIError(kind: .decoding, message: "Couldn't read your availability.")
        }
        let sent = c.staffArray(.week, of: StaffAvailabilityDay.self)
        // Always seven, Monday first; a day the server left out is "any".
        week = Self.weekdays.map { d in sent.first { $0.day.lowercased() == d.lowercased() } ?? StaffAvailabilityDay(day: d) }
        unavailableDays = c.staffArray(.unavailableDays)
        notes = c.staffString(.notes) ?? ""
        updatedAt = c.staffString(.updatedAt)
        timeOffHint = c.staffString(.timeOffHint)
    }
}

struct StaffAvailabilityConflict: Decodable, Identifiable, Hashable {
    let date: String
    let shiftStart: String
    let shiftEnd: String?
    let role: String?
    let reason: String?

    var id: String { date + "|" + shiftStart }
    var label: String { StaffDay.shift(date, shiftStart, shiftEnd) }

    enum CodingKeys: String, CodingKey {
        case date, role, reason
        case shiftStart = "shift_start", shiftEnd = "shift_end"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = c.staffString(.date) ?? ""
        shiftStart = c.staffString(.shiftStart) ?? ""
        shiftEnd = c.staffString(.shiftEnd)
        role = c.staffString(.role)
        reason = c.staffString(.reason)
    }
}

/// The 200 answer: the record as stored now (new `updated_at`), the
/// published shifts it rules out, and the time-off hint.
struct StaffAvailabilitySaved: Decodable {
    let record: StaffAvailabilityRecord
    let conflicts: [StaffAvailabilityConflict]
    let conflictsText: String
    let hint: StaffHint?

    enum CodingKeys: String, CodingKey {
        case conflicts, hint
        case conflictsText = "conflicts_text"
    }

    init(from decoder: Decoder) throws {
        record = try StaffAvailabilityRecord(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        conflicts = c.staffArray(.conflicts)
        conflictsText = c.staffString(.conflictsText) ?? ""
        hint = try? c.decodeIfPresent(StaffHint.self, forKey: .hint)
    }
}

/// A refusal's body: 409 `{stale: true, availability}` or 400 `{error, hint?}`.
struct StaffAvailabilityRefusal: Decodable {
    let error: String?
    let stale: Bool
    let availability: StaffAvailabilityRecord?
    let hint: StaffHint?

    enum CodingKeys: String, CodingKey { case error, stale, availability, hint }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        error = c.staffString(.error)
        stale = c.staffBool(.stale)
        availability = try? c.decodeIfPresent(StaffAvailabilityRecord.self, forKey: .availability)
        hint = try? c.decodeIfPresent(StaffHint.self, forKey: .hint)
    }
}

struct StaffAvailabilitySaveBody: Encodable {
    let updatedAt: String?
    let week: [StaffAvailabilityDay]
    let notes: String

    enum CodingKeys: String, CodingKey {
        case week, notes
        case updatedAt = "updated_at"
    }

    /// `updated_at` is always present — JSON null when the record is new.
    /// Leaving the key out is itself a 409 (PERF-05's guard).
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(updatedAt, forKey: .updatedAt)
        try c.encode(week, forKey: .week)
        try c.encode(notes, forKey: .notes)
    }
}

/// The editor's working copy and its checks — the server's rules said
/// before the save, under the day they're about.
struct StaffAvailabilityDraft: Equatable {
    var days: [StaffAvailabilityDay]
    var notes: String
    let updatedAt: String?

    init(_ record: StaffAvailabilityRecord) {
        days = record.week
        notes = record.notes
        updatedAt = record.updatedAt
    }

    /// Day → the one sentence that stops the save; "" for the whole week.
    func problems(today: String) -> [String: String] {
        var out: [String: String] = [:]
        for d in days where d.status != .any {
            if d.status == .window, d.earliest == nil, d.latest == nil {
                out[d.day] = "Pick the earliest start or the latest finish."
                continue
            }
            if let u = d.until, u < today {
                out[d.day] = "That end date has passed."
                continue
            }
            if let f = d.from, let u = d.until, f > u {
                out[d.day] = "The end date is before the start date."
            }
        }
        if days.allSatisfy({ $0.status == .off && $0.from == nil && $0.until == nil }) {
            out[""] = "Every day blocked — leave at least one you can work."
        }
        return out
    }

    func body() -> StaffAvailabilitySaveBody {
        StaffAvailabilitySaveBody(updatedAt: updatedAt, week: days,
                                  notes: String(notes.trimmingCharacters(in: .whitespacesAndNewlines).prefix(300)))
    }

    /// "2 days off · 1 with hours" for the Me row.
    static func rowSummary(_ record: StaffAvailabilityRecord) -> String {
        let off = record.week.filter { $0.status == .off }.count
        let hours = record.week.filter { $0.status == .window }.count
        if off == 0 && hours == 0 { return "Any day" }
        var parts: [String] = []
        if off > 0 { parts.append(off == 1 ? "1 day off" : "\(off) days off") }
        if hours > 0 { parts.append(hours == 1 ? "1 with hours" : "\(hours) with hours") }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Preferences and notices (GET/POST /staff/api/preferences, /notifications)

struct StaffPreferencesState: Decodable, Equatable {
    let preferredDayparts: [String]
    let desiredHours: Double?
    let scheduleTexts: Bool
    /// False while the staff text service isn't set up: the switch is hidden
    /// (H14) — it would promise texts that can't go.
    let smsAvailable: Bool
    let consentText: String?
    let consentVersion: Int?
    let scope: [String]

    enum CodingKeys: String, CodingKey {
        case ok, error
        case preferredDayparts = "preferred_dayparts", desiredHours = "desired_hours"
        case scheduleTexts = "schedule_texts", smsAvailable = "sms_available"
        case consentText = "schedule_texts_consent", consentVersion = "schedule_texts_consent_version"
        case scope = "schedule_texts_scope"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your preferences.")
        }
        preferredDayparts = c.staffArray(.preferredDayparts)
        if let d = try? c.decodeIfPresent(Double.self, forKey: .desiredHours) {
            desiredHours = d
        } else if let s = c.staffString(.desiredHours) {
            desiredHours = Double(s)
        } else {
            desiredHours = nil
        }
        scheduleTexts = c.staffBool(.scheduleTexts)
        smsAvailable = c.staffBool(.smsAvailable)
        consentText = c.staffString(.consentText)
        consentVersion = c.staffInt(.consentVersion)
        scope = c.staffArray(.scope)
    }
}

/// {schedule_texts, consent_version} — the switch alone; the server records
/// the scope the wording shown covers (v2: schedule and requests).
struct StaffTextsConsentBody: Encodable, Equatable {
    let scheduleTexts: Bool
    let consentVersion: Int

    enum CodingKeys: String, CodingKey {
        case scheduleTexts = "schedule_texts", consentVersion = "consent_version"
    }
}

struct StaffPreferencesSaveBody: Encodable, Equatable {
    let preferredDayparts: [String]
    let desiredHours: Int?

    enum CodingKeys: String, CodingKey {
        case preferredDayparts = "preferred_dayparts", desiredHours = "desired_hours"
    }

    /// desired_hours goes as null when cleared, as the web did.
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(preferredDayparts, forKey: .preferredDayparts)
        try c.encode(desiredHours, forKey: .desiredHours)
    }
}

struct StaffNotificationSettings: Decodable, Equatable {
    let pushRegistered: Bool
    let reminders: Bool
    let quietStart: String?
    let quietEnd: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, reminders
        case pushRegistered = "push_registered", quietHours = "quiet_hours"
    }

    private struct Quiet: Decodable { let start: String?; let end: String? }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your notices.")
        }
        pushRegistered = c.staffBool(.pushRegistered)
        reminders = c.staffBool(.reminders, default: true)
        let q = try? c.decodeIfPresent(Quiet.self, forKey: .quietHours)
        quietStart = q?.start
        quietEnd = q?.end
    }

    /// "Quiet 10pm – 8am: notices arrive silently."
    var quietLine: String? {
        guard let quietStart, let quietEnd else { return nil }
        return "Quiet \(StaffClock.display(quietStart)) – \(StaffClock.display(quietEnd)): notices arrive silently."
    }
}

// MARK: - Me (GET /staff/api/me — the B1 fields)

struct StaffLocation: Decodable, Identifiable, Hashable {
    let restaurantId: Int
    let restaurant: String
    let membershipId: Int?
    let current: Bool

    var id: Int { restaurantId }

    enum CodingKeys: String, CodingKey {
        case restaurant, current
        case restaurantId = "restaurant_id", membershipId = "membership_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        restaurantId = c.staffInt(.restaurantId) ?? 0
        restaurant = c.staffString(.restaurant) ?? ""
        membershipId = c.staffInt(.membershipId)
        current = c.staffBool(.current)
    }
}

struct StaffMeDetails: Decodable, Hashable {
    let name: String
    let restaurant: String
    let phoneMasked: String
    let hasPhone: Bool
    let email: String
    let pushOn: Bool
    let textsOn: Bool
    let locations: [StaffLocation]

    enum CodingKeys: String, CodingKey {
        case name, restaurant, email, notifications, locations
        case phoneMasked = "phone_masked", hasPhone = "has_phone"
    }

    private struct Channels: Decodable {
        let push: Bool?
        let texts: Bool?
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.staffString(.name) ?? ""
        restaurant = c.staffString(.restaurant) ?? ""
        phoneMasked = c.staffString(.phoneMasked) ?? ""
        hasPhone = c.staffBool(.hasPhone)
        email = c.staffString(.email) ?? ""
        let ch = try? c.decodeIfPresent(Channels.self, forKey: .notifications)
        pushOn = ch?.push ?? false
        textsOn = ch?.texts ?? false
        locations = c.staffArray(.locations)
    }
}

struct StaffMeEnvelope: Decodable {
    let employee: StaffMeDetails

    enum CodingKeys: String, CodingKey { case ok, error, employee }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard (try? c.decode(Bool.self, forKey: .ok)) != false,
              let e = try? c.decode(StaffMeDetails.self, forKey: .employee) else {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your details.")
        }
        employee = e
    }
}

// MARK: - Inbox (GET /staff/api/inbox, POST …/announcements/<id>/ack)

struct StaffAnnouncement: Decodable, Identifiable, Hashable {
    let id: Int
    let title: String
    let body: String
    let priority: String
    let createdAt: String?
    let createdByName: String
    let expiresOn: String?
    var ackedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, title, body, priority
        case createdAt = "created_at", createdByName = "created_by_name"
        case expiresOn = "expires_on", ackedAt = "acked_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        title = c.staffString(.title) ?? ""
        body = c.staffString(.body) ?? ""
        priority = c.staffString(.priority) ?? "normal"
        createdAt = c.staffString(.createdAt)
        createdByName = c.staffString(.createdByName) ?? ""
        expiresOn = c.staffString(.expiresOn)
        ackedAt = c.staffString(.ackedAt)
    }

    var isUrgent: Bool { priority == "urgent" }
    var isRead: Bool { ackedAt != nil }

    /// "Erik S · 9/21/26 · 6:45pm" — who sent it and when, on this phone.
    var byline: String {
        let when = createdAt.flatMap(CavnarDate.timestamp).map { CavnarDate.mdyTime($0) } ?? ""
        return [createdByName, when].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

struct StaffInboxPayload: Decodable, Equatable {
    let announcements: [StaffAnnouncement]
    let unread: Int
    let unreadMessages: Int

    enum CodingKeys: String, CodingKey {
        case ok, error, announcements, unread
        case unreadMessages = "unread_messages"
    }

    init(announcements: [StaffAnnouncement], unread: Int, unreadMessages: Int) {
        self.announcements = announcements; self.unread = unread; self.unreadMessages = unreadMessages
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your inbox.")
        }
        let list: [StaffAnnouncement] = c.staffArray(.announcements)
        announcements = list
        unread = c.staffInt(.unread) ?? list.filter { !$0.isRead }.count
        unreadMessages = c.staffInt(.unreadMessages) ?? 0
    }

    /// The Inbox tab's count: announcements not yet "Got it" plus manager
    /// replies not yet read (B5's badge rule).
    var badge: Int { unread + unreadMessages }

    /// The same payload with one announcement marked read — the ack lands
    /// on screen without a refetch.
    func acking(_ id: Int, at stamp: String) -> StaffInboxPayload {
        var changed = false
        let list = announcements.map { a -> StaffAnnouncement in
            guard a.id == id, a.ackedAt == nil else { return a }
            var b = a
            b.ackedAt = stamp
            changed = true
            return b
        }
        return StaffInboxPayload(announcements: list, unread: max(0, unread - (changed ? 1 : 0)),
                                 unreadMessages: unreadMessages)
    }
}

struct StaffAckResponse: Decodable {
    let ackedAt: String?
    enum CodingKeys: String, CodingKey { case ackedAt = "acked_at" }
}

// MARK: - The manager thread (GET/POST /staff/api/messages)

/// What a message is about, when it is about something: a shift (its date)
/// or one of my requests. Shown above the composer and sent with each
/// message until the person drops it.
struct StaffMessageContext: Hashable {
    var shiftDate: String?
    var requestId: Int?
    /// "shift" or "time_off".
    var requestKind: String?
    /// "Sat 9/27/26 · 4pm – 10pm" / "Time off 10/3/26 – 10/6/26".
    var label: String

    init(shiftDate: String? = nil, requestId: Int? = nil, requestKind: String? = nil, label: String) {
        self.shiftDate = shiftDate; self.requestId = requestId; self.requestKind = requestKind; self.label = label
    }

    static func shift(date: String, start: String? = nil, end: String? = nil) -> StaffMessageContext {
        StaffMessageContext(shiftDate: date, label: StaffDay.shift(date, start, end))
    }

    static func shiftRequest(_ r: StaffMyShiftRequest) -> StaffMessageContext {
        StaffMessageContext(shiftDate: r.date, requestId: r.id, requestKind: "shift",
                            label: "\(r.tag): \(r.shiftLabel)")
    }

    static func timeOff(_ t: StaffTimeOffRequest) -> StaffMessageContext {
        StaffMessageContext(requestId: t.id, requestKind: "time_off", label: "Time off \(t.rangeLabel)")
    }
}

struct StaffThreadMessage: Decodable, Identifiable, Hashable {
    let id: Int
    let from: String
    let senderName: String
    let body: String
    let shiftDate: String?
    let requestId: Int?
    let requestKind: String?
    let createdAt: String?
    let readAt: String?

    enum CodingKeys: String, CodingKey {
        case id, from, body
        case senderName = "sender_name", shiftDate = "shift_date", requestId = "request_id"
        case requestKind = "request_kind", createdAt = "created_at", readAt = "read_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        from = c.staffString(.from) ?? "staff"
        senderName = c.staffString(.senderName) ?? ""
        body = c.staffString(.body) ?? ""
        shiftDate = c.staffString(.shiftDate)
        requestId = c.staffInt(.requestId)
        requestKind = c.staffString(.requestKind)
        createdAt = c.staffString(.createdAt)
        readAt = c.staffString(.readAt)
    }

    var isMine: Bool { from == "staff" }

    var timeLabel: String {
        createdAt.flatMap(CavnarDate.timestamp).map { CavnarDate.mdyTime($0) } ?? ""
    }

    /// "About Sat 9/27/26" — the context a message carried.
    var contextLabel: String? {
        if requestKind == "time_off" { return "About a time-off request" }
        if let d = shiftDate { return "About \(StaffDay.shift(d, nil))" }
        if requestId != nil { return "About a shift request" }
        return nil
    }
}

struct StaffThreadPayload: Decodable, Equatable {
    let threadId: Int?
    let unread: Int
    let messages: [StaffThreadMessage]

    enum CodingKeys: String, CodingKey {
        case ok, error, unread, messages
        case threadId = "thread_id"
    }

    init(threadId: Int?, unread: Int, messages: [StaffThreadMessage]) {
        self.threadId = threadId; self.unread = unread; self.messages = messages
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your messages.")
        }
        threadId = c.staffInt(.threadId)
        unread = c.staffInt(.unread) ?? 0
        messages = c.staffArray(.messages)
    }

    /// The last of my messages a manager has read — where "Seen" goes.
    var lastSeenMineId: Int? {
        messages.last(where: { $0.isMine && $0.readAt != nil })?.id
    }

    func appending(_ m: StaffThreadMessage) -> StaffThreadPayload {
        guard !messages.contains(where: { $0.id == m.id }) else { return self }
        return StaffThreadPayload(threadId: threadId, unread: unread, messages: messages + [m])
    }
}

struct StaffMessageBody: Encodable, Equatable {
    let body: String
    let shiftDate: String?
    let requestId: Int?
    let requestKind: String?

    enum CodingKeys: String, CodingKey {
        case body
        case shiftDate = "shift_date", requestId = "request_id", requestKind = "request_kind"
    }

    init(body: String, context: StaffMessageContext?) {
        self.body = body
        shiftDate = context?.shiftDate
        requestId = context?.requestId
        requestKind = context?.requestId == nil ? nil : context?.requestKind
    }

    /// The context keys only when there is context (the server validates
    /// whatever it is sent).
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(body, forKey: .body)
        try c.encodeIfPresent(shiftDate, forKey: .shiftDate)
        try c.encodeIfPresent(requestId, forKey: .requestId)
        try c.encodeIfPresent(requestKind, forKey: .requestKind)
    }
}

struct StaffMessageSent: Decodable {
    let message: StaffThreadMessage
}

// MARK: - Post-shift pulse (GET/POST /staff/api/pulse, V6)

struct StaffPulseDue: Decodable, Hashable {
    let date: String
    let dateLabel: String
    let start: String?
    let role: String?

    enum CodingKeys: String, CodingKey {
        case date, start, role
        case dateLabel = "date_label"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = c.staffString(.date) ?? ""
        dateLabel = c.staffString(.dateLabel) ?? CavnarDate.mdy(date)
        start = c.staffString(.start)
        role = c.staffString(.role)
    }

    /// "Fri 9/26/26 · 4pm · Server".
    var line: String {
        [StaffDay.shift(date, start), role ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

struct StaffPulseState: Decodable {
    let due: StaffPulseDue?

    enum CodingKeys: String, CodingKey { case ok, error, due }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load.")
        }
        due = try? c.decodeIfPresent(StaffPulseDue.self, forKey: .due)
    }
}

struct StaffPulseBody: Encodable, Equatable {
    let date: String
    let rating: Int
    let note: String?

    enum CodingKeys: String, CodingKey { case date, rating, note }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(date, forKey: .date)
        try c.encode(rating, forKey: .rating)
        try c.encodeIfPresent(note, forKey: .note)
    }
}

enum StaffPulseScale {
    static let ratings = [1, 2, 3, 4, 5]

    static func word(_ r: Int) -> String {
        switch r {
        case 1: return "Rough"
        case 2: return "Hard"
        case 3: return "OK"
        case 4: return "Good"
        default: return "Great"
        }
    }
}

// MARK: - Calendar link (GET /staff/api/calendar-link, M11)

struct StaffCalendarLink: Decodable, Equatable {
    let url: String
    let webcalURL: String
    let lastFetchedAt: String?
    let note: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, url, note
        case webcalURL = "webcal_url", lastFetchedAt = "last_fetched_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard (try? c.decode(Bool.self, forKey: .ok)) != false, let u = c.staffString(.url), !u.isEmpty else {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your calendar link.")
        }
        url = u
        webcalURL = c.staffString(.webcalURL)
            ?? (u.hasPrefix("https://") ? "webcal://" + u.dropFirst(8) : u)
        lastFetchedAt = c.staffString(.lastFetchedAt)
        note = c.staffString(.note)
    }
}

// MARK: - Language (GET/POST /staff/api/language, V11)

struct StaffLanguageChoice: Decodable, Identifiable, Hashable {
    let code: String
    let name: String
    var id: String { code }
}

struct StaffLanguages: Decodable, Equatable {
    let language: String
    let languages: [StaffLanguageChoice]

    enum CodingKeys: String, CodingKey { case ok, error, language, languages }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load languages.")
        }
        language = c.staffString(.language) ?? "en"
        languages = c.staffArray(.languages)
    }

    var currentName: String { languages.first { $0.code == language }?.name ?? "English" }
}

// MARK: - Docs, house rules and asking them (GET /staff/api/docs, POST /ask, V9/V10)

struct StaffDoc: Decodable, Identifiable, Hashable {
    let id: Int
    let kind: String
    let kindLabel: String?
    let title: String
    let body: String
    let updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, title, body
        case kindLabel = "kind_label", updatedAt = "updated_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.staffInt(.id) ?? 0
        kind = c.staffString(.kind) ?? ""
        kindLabel = c.staffString(.kindLabel)
        title = c.staffString(.title) ?? ""
        body = c.staffString(.body) ?? ""
        updatedAt = c.staffString(.updatedAt)
    }
}

struct StaffCert: Decodable, Identifiable, Hashable {
    let cert: String
    let expiresOn: String?
    let daysLeft: Int?
    let status: String

    var id: String { cert }

    enum CodingKeys: String, CodingKey {
        case cert, status
        case expiresOn = "expires_on", daysLeft = "days_left"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cert = c.staffString(.cert) ?? ""
        expiresOn = c.staffString(.expiresOn)
        daysLeft = c.staffInt(.daysLeft)
        status = c.staffString(.status) ?? "no_expiry"
    }

    var chip: StaffStatusChip {
        switch status {
        case "expired": return StaffStatusChip(text: "Expired", tone: .bad)
        case "expiring": return StaffStatusChip(text: "Expiring", tone: .warning)
        case "current": return StaffStatusChip(text: "Current", tone: .good)
        default: return StaffStatusChip(text: "No expiry on file", tone: .neutral)
        }
    }

    var line: String? {
        guard let e = expiresOn else { return nil }
        return status == "expired" ? "Expired \(CavnarDate.mdy(e))" : "Expires \(CavnarDate.mdy(e))"
    }
}

struct StaffDocsPayload: Decodable, Equatable {
    let houseRules: StaffDoc?
    let docs: [StaffDoc]
    let certifications: [StaffCert]
    let canAsk: Bool

    enum CodingKeys: String, CodingKey {
        case ok, error, docs, certifications
        case houseRules = "house_rules", canAsk = "can_ask"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if (try? c.decode(Bool.self, forKey: .ok)) == false {
            throw APIClient.APIError(message: c.staffString(.error) ?? "Couldn't load your docs.")
        }
        houseRules = try? c.decodeIfPresent(StaffDoc.self, forKey: .houseRules)
        docs = c.staffArray(.docs)
        certifications = c.staffArray(.certifications)
        canAsk = c.staffBool(.canAsk)
    }

    var isEmpty: Bool { houseRules == nil && docs.isEmpty && certifications.isEmpty }
}

struct StaffAskSource: Decodable, Identifiable, Hashable {
    let id: String
    let source: String
    let kind: String
    let line: String
}

struct StaffAskAnswer: Decodable, Equatable {
    let answered: Bool
    let answer: String
    let reason: String?
    let suggestMessage: Bool
    let sources: [StaffAskSource]

    enum CodingKeys: String, CodingKey {
        case answered, answer, reason, sources, error
        case suggestMessage = "suggest_message"
    }

    init(answered: Bool, answer: String, reason: String? = nil, suggestMessage: Bool, sources: [StaffAskSource] = []) {
        self.answered = answered; self.answer = answer; self.reason = reason
        self.suggestMessage = suggestMessage; self.sources = sources
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        answered = c.staffBool(.answered)
        answer = c.staffString(.answer) ?? c.staffString(.error) ?? ""
        reason = c.staffString(.reason)
        suggestMessage = c.staffBool(.suggestMessage)
        sources = c.staffArray(.sources)
    }
}

struct StaffQuestionBody: Encodable, Equatable {
    let question: String
}

// MARK: - Load state

/// One screen section's load: never "loaded and empty" for a failure.
enum StaffLoad<Value> {
    case loading
    case failed(String)
    case loaded(Value)

    var value: Value? {
        if case .loaded(let v) = self { return v }
        return nil
    }

    var failure: String? {
        if case .failed(let m) = self { return m }
        return nil
    }

    var isLoading: Bool {
        if case .loading = self { return true }
        return false
    }
}

enum StaffErrorText {
    /// The server's sentence when it sent one; a plain one otherwise.
    static func message(_ error: Error, fallback: String = "That didn't go through.") -> String {
        if let e = error as? APIClient.APIError {
            switch e.kind {
            case .offline: return "You're offline — check your connection and try again."
            case .timedOut: return "That took too long — try again."
            case .decoding: return fallback
            default: return e.message
            }
        }
        if error is APIClient.SessionExpiredError { return "Your shift session ended — sign in again." }
        return fallback
    }
}
