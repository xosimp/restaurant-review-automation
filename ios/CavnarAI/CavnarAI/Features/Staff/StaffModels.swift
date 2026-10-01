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

struct StaffShift: Decodable, Hashable {
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

    enum CodingKeys: String, CodingKey {
        case date, day, role, notes, station, start, end, hours
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case scheduledHours = "scheduled_hours"
    }

    /// /staff/api/shifts sends `start`, `end` and `hours`
    /// (labor.employee_shifts_from_csv); older payloads and the schedule
    /// views send `shift_start`, `shift_end`, `scheduled_hours`. Both decode —
    /// reading only the second left every staff shift with no times (9/30/26).
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = try c.decodeIfPresent(String.self, forKey: .date)
        day = try c.decodeIfPresent(String.self, forKey: .day)
        role = try c.decodeIfPresent(String.self, forKey: .role)
        notes = try c.decodeIfPresent(String.self, forKey: .notes)
        station = try c.decodeIfPresent(String.self, forKey: .station)
        shiftStart = try c.decodeIfPresent(String.self, forKey: .shiftStart)
            ?? c.decodeIfPresent(String.self, forKey: .start)
        shiftEnd = try c.decodeIfPresent(String.self, forKey: .shiftEnd)
            ?? c.decodeIfPresent(String.self, forKey: .end)
        if let h = try? c.decodeIfPresent(String.self, forKey: .scheduledHours) {
            scheduledHours = h
        } else if let h = try? c.decodeIfPresent(Double.self, forKey: .hours) {
            scheduledHours = h == h.rounded() ? String(Int(h)) : String(h)
        } else {
            scheduledHours = try? c.decodeIfPresent(String.self, forKey: .hours)
        }
    }

    var timeRange: String {
        let start = shiftStart ?? ""
        let end = shiftEnd ?? ""
        guard !start.isEmpty || !end.isEmpty else { return "" }
        return "\(start) – \(end)"
    }
}

struct StaffWeekDay: Decodable, Identifiable, Hashable {
    let date: String
    let weekday: String
    let isToday: Bool
    let off: Bool
    let shift: StaffShift?

    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, weekday, off, shift
        case isToday = "is_today"
    }
}

struct StaffShiftsResponse: Decodable {
    let ok: Bool
    /// False when the restaurant has never published a schedule. The portal
    /// says so plainly rather than showing anything invented — the backend
    /// deliberately does not fall back to the sample shift data the owner
    /// dashboard uses for previews.
    let published: Bool?
    let today: StaffShift?
    let week: [StaffWeekDay]?
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

    enum CodingKeys: String, CodingKey {
        case ok, role, tasks, sheets, manager, floor, signoffs
        case canSignOff = "can_sign_off"
    }
}

struct StaffProfile: Decodable, Hashable {
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
