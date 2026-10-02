import Foundation

/// What the staff "Next shift" widget shows (MISS-11): the signed-in
/// employee's own shifts for the seven days the app last read
/// (/staff/api/shifts), so a phone shows "Next shift: Fri 4pm – 10pm ·
/// Bartender" without anyone opening the app.
///
/// Compiled into BOTH the app and the widget extension, like
/// WidgetSnapshot: the app writes it (WidgetSnapshotService.refreshStaff),
/// the widget only reads it, never calls the API and never holds a token.
/// Only the stable fields of a shift are kept — date, start, end, role.
struct StaffShiftSnapshot: Codable, Equatable {
    struct Shift: Codable, Equatable {
        /// ISO date, the restaurant's service day ("2026-10-02").
        let date: String
        /// As the schedule says them ("4:00pm", "16:00").
        let start: String?
        let end: String?
        let role: String?
    }

    /// False when the restaurant has never published a schedule.
    var published: Bool
    /// The shifts in the window, in order.
    var shifts: [Shift]
    /// The first day the read covered (the restaurant's today then), ISO.
    var windowStart: String
    var updatedAt: Date

    static let storageKey = "cavnar.widget.staff-shift.v1"
    static let widgetKind = "CavnarNextShiftWidget"
    /// /staff/api/shifts reads seven days from today.
    static let windowDays = 7

    /// What the widget can honestly say at `now`.
    enum Answer: Equatable {
        case next(Shift)
        /// No shift in the next seven days (the read is today's).
        case none
        /// No shift through this day — an older read covers fewer days ahead.
        case noneThrough(String)
        case notPublished
        /// Nothing current to go on: never read, or the read's days are past.
        case unknown
    }

    func answer(now: Date = Date(), calendar: Calendar = .current) -> Answer {
        guard let first = Self.day(windowStart, calendar: calendar),
              let windowEnd = calendar.date(byAdding: .day, value: Self.windowDays, to: first) else { return .unknown }
        let today = calendar.startOfDay(for: now)
        guard today < windowEnd else { return .unknown }
        guard published else { return .notPublished }
        for shift in shifts {
            guard let d = Self.day(shift.date, calendar: calendar) else { continue }
            if d > today { return .next(shift) }
            let end = Self.endDate(shift, on: d, calendar: calendar)
            if calendar.isDate(d, inSameDayAs: today) {
                if let end, now >= end { continue }
                return .next(shift)
            }
            // Yesterday's close that runs past midnight is still on.
            if let end, now < end { return .next(shift) }
        }
        if calendar.isDate(first, inSameDayAs: today) { return .none }
        guard let last = calendar.date(byAdding: .day, value: -1, to: windowEnd) else { return .unknown }
        return .noneThrough(Self.weekday(last, calendar: calendar))
    }

    /// The widget's one sentence.
    func line(now: Date = Date(), calendar: Calendar = .current) -> String {
        switch answer(now: now, calendar: calendar) {
        case .next(let s): return "Next shift: " + Self.describe(s, now: now, calendar: calendar)
        case .none: return "No shifts in the next 7 days"
        case .noneThrough(let day): return "No shifts through \(day)"
        case .notPublished: return "No schedule posted yet"
        case .unknown: return "Open Cavnar AI to see your next shift"
        }
    }

    /// "Fri 4pm – 10pm · Bartender"; "Today" / "Tomorrow" when it is.
    static func describe(_ s: Shift, now: Date = Date(), calendar: Calendar = .current) -> String {
        var out = dayWord(s.date, now: now, calendar: calendar)
        let times = timeRange(s)
        if !times.isEmpty { out += " " + times }
        if let role = s.role?.trimmingCharacters(in: .whitespaces), !role.isEmpty { out += " · " + role }
        return out
    }

    /// "4pm – 10pm", "" when the schedule gave no times.
    static func timeRange(_ s: Shift) -> String {
        let a = s.start.map(compactTime) ?? ""
        let b = s.end.map(compactTime) ?? ""
        if a.isEmpty && b.isEmpty { return "" }
        if b.isEmpty { return a }
        if a.isEmpty { return "until " + b }
        return "\(a) – \(b)"
    }

    static func dayWord(_ iso: String, now: Date, calendar: Calendar) -> String {
        guard let d = day(iso, calendar: calendar) else { return iso }
        if calendar.isDate(d, inSameDayAs: now) { return "Today" }
        if let tomorrow = calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: now)),
           calendar.isDate(d, inSameDayAs: tomorrow) { return "Tomorrow" }
        return weekday(d, calendar: calendar)
    }

    static func weekday(_ d: Date, calendar: Calendar) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = calendar
        f.timeZone = calendar.timeZone
        f.dateFormat = "EEE"
        return f.string(from: d)
    }

    /// "4:00pm" → "4pm", "4:30 PM" → "4:30pm", "16:00" → "4pm".
    static func compactTime(_ raw: String) -> String {
        guard let (h, m) = clock(raw) else { return raw.trimmingCharacters(in: .whitespaces) }
        let hour = h % 12 == 0 ? 12 : h % 12
        return "\(hour)\(m == 0 ? "" : String(format: ":%02d", m))\(h >= 12 ? "pm" : "am")"
    }

    /// (hour 0–23, minute) from "4:00pm", "4pm", "4:30 PM", "16:00", "9a".
    static func clock(_ raw: String) -> (Int, Int)? {
        var t = raw.lowercased().replacingOccurrences(of: " ", with: "").replacingOccurrences(of: ".", with: "")
        var meridiem: String?
        for suffix in ["am", "pm", "a", "p"] where t.hasSuffix(suffix) {
            meridiem = suffix.hasPrefix("a") ? "am" : "pm"
            t.removeLast(suffix.count)
            break
        }
        let parts = t.split(separator: ":")
        guard let first = parts.first, var h = Int(first), parts.count <= 2 else { return nil }
        let m = parts.count == 2 ? (Int(parts[1]) ?? -1) : 0
        guard (0..<60).contains(m) else { return nil }
        if let meridiem {
            guard (1...12).contains(h) else { return nil }
            if meridiem == "am" { h = h == 12 ? 0 : h } else { h = h == 12 ? 12 : h + 12 }
        }
        guard (0..<24).contains(h) else { return nil }
        return (h, m)
    }

    /// When a shift on `day` ends — the next day for one that ends at or
    /// before it starts (a close past midnight). Nil without an end time.
    static func endDate(_ s: Shift, on day: Date, calendar: Calendar) -> Date? {
        guard let end = s.end.flatMap(clock),
              var at = calendar.date(bySettingHour: end.0, minute: end.1, second: 0, of: day) else { return nil }
        if let start = s.start.flatMap(clock), end.0 * 60 + end.1 <= start.0 * 60 + start.1 {
            at = calendar.date(byAdding: .day, value: 1, to: at) ?? at
        }
        return at
    }

    static func day(_ iso: String, calendar: Calendar) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = calendar
        f.timeZone = calendar.timeZone
        f.dateFormat = "yyyy-MM-dd"
        return f.date(from: String(iso.prefix(10))).map { calendar.startOfDay(for: $0) }
    }

    /// The moments the widget's answer can change after `now`: each shift's
    /// end and each midnight in the window. The widget's timeline steps on
    /// these.
    func changePoints(after now: Date, calendar: Calendar = .current) -> [Date] {
        var points: [Date] = []
        for s in shifts {
            if let d = Self.day(s.date, calendar: calendar), let end = Self.endDate(s, on: d, calendar: calendar),
               end > now { points.append(end) }
        }
        if let first = Self.day(windowStart, calendar: calendar) {
            for i in 1...Self.windowDays {
                if let midnight = calendar.date(byAdding: .day, value: i, to: first), midnight > now {
                    points.append(midnight)
                }
            }
        }
        return Array(Set(points)).sorted()
    }

    // MARK: Storage (the app group both targets share)

    private static var defaults: UserDefaults? { UserDefaults(suiteName: WidgetSnapshot.appGroup) }

    static func load() -> StaffShiftSnapshot? {
        guard let data = defaults?.data(forKey: storageKey) else { return nil }
        return try? JSONDecoder().decode(StaffShiftSnapshot.self, from: data)
    }

    static func save(_ snapshot: StaffShiftSnapshot) {
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        defaults?.set(data, forKey: storageKey)
    }

    /// Signed out: the widget stops showing anyone's shifts.
    static func clear() {
        defaults?.removeObject(forKey: storageKey)
    }
}
