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
}

struct StaffLoginResponse: Decodable {
    let ok: Bool
    /// The shift session. The web client gets this as an httponly cookie; a
    /// native client has no cookie jar, so the same value comes back in the
    /// body and goes straight to the Keychain.
    let token: String?
    let error: String?
    let locked: Bool?
}

struct StaffShift: Decodable, Hashable {
    let date: String?
    let day: String?
    let role: String?
    let shiftStart: String?
    let shiftEnd: String?
    let scheduledHours: String?
    let notes: String?

    enum CodingKeys: String, CodingKey {
        case date, day, role, notes
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case scheduledHours = "scheduled_hours"
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
