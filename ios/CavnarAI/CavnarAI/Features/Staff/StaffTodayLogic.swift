import Foundation

// The pure rules behind the staff app's Today screen and its tab frame
// (employee audit V1, C6, H6, UX-09/10). No SwiftUI here: everything is a
// function of the payload and a clock, so StaffTodayTests can pin it.

// MARK: - Times

/// Shift times as the house writes them: "4pm" on the hour, "4:30pm"
/// otherwise (DESIGN_SYSTEM → Dates and times), whatever spelling the
/// schedule CSV used ("4:00pm", "4pm", "16:00", "4:00 PM").
enum StaffTime {
    /// Minutes past midnight, or nil when unreadable.
    static func minutes(_ raw: String) -> Int? {
        let s = raw.lowercased().replacingOccurrences(of: " ", with: "")
            .replacingOccurrences(of: ".", with: "")
        guard !s.isEmpty else { return nil }
        var body = s
        var meridiem: String?
        if body.hasSuffix("am") || body.hasSuffix("pm") {
            meridiem = String(body.suffix(2))
            body = String(body.dropLast(2))
        }
        let parts = body.split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count >= 1, parts.count <= 3, let h = Int(parts[0]) else { return nil }
        let m = parts.count >= 2 ? Int(parts[1]) : 0
        guard let m, (0..<60).contains(m) else { return nil }
        if let meridiem {
            guard (1...12).contains(h) else { return nil }
            let h24 = (h % 12) + (meridiem == "pm" ? 12 : 0)
            return h24 * 60 + m
        }
        guard (0...24).contains(h) else { return nil }
        return (h % 24) * 60 + m
    }

    /// "4pm", "4:30pm", "12am"; the input as given when unreadable.
    static func label(_ raw: String) -> String {
        guard let m = minutes(raw) else { return raw.trimmingCharacters(in: .whitespaces) }
        return label(minutes: m)
    }

    static func label(minutes total: Int) -> String {
        let m = ((total % 1440) + 1440) % 1440
        let h = m / 60, mm = m % 60
        let h12 = h % 12 == 0 ? 12 : h % 12
        let suffix = h < 12 ? "am" : "pm"
        return mm == 0 ? "\(h12)\(suffix)" : "\(h12):\(String(format: "%02d", mm))\(suffix)"
    }

    /// "6 hrs", "6.5 hrs", "1 hr"; nil when unknown or zero.
    static func hoursLabel(_ hours: Double?) -> String? {
        guard let hours, hours > 0, hours.isFinite else { return nil }
        let rounded = (hours * 10).rounded() / 10
        let text = rounded == rounded.rounded() ? String(Int(rounded)) : String(rounded)
        return rounded == 1 ? "1 hr" : "\(text) hrs"
    }

    /// "32" / "31.5" — a figure for a tile, one decimal at most.
    static func figure(_ hours: Double) -> String {
        let rounded = (hours * 10).rounded() / 10
        return rounded == rounded.rounded() ? String(Int(rounded)) : String(rounded)
    }

    /// The moment a leg starts and ends on `dayISO` (yyyy-MM-dd) in
    /// `calendar`'s zone; an end at or before the start runs past midnight.
    static func span(dayISO: String, start: String?, end: String?,
                     calendar: Calendar) -> (start: Date, end: Date?)? {
        guard let base = day(dayISO, calendar: calendar),
              let s = start.flatMap(minutes) else { return nil }
        let startDate = base.addingTimeInterval(TimeInterval(s * 60))
        guard let e = end.flatMap(minutes) else { return (startDate, nil) }
        var endDate = base.addingTimeInterval(TimeInterval(e * 60))
        if endDate <= startDate { endDate = endDate.addingTimeInterval(24 * 3600) }
        return (startDate, endDate)
    }

    /// Midnight of an ISO day in `calendar`'s zone.
    static func day(_ iso: String, calendar: Calendar) -> Date? {
        let p = iso.prefix(10).split(separator: "-")
        guard p.count == 3, let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]) else { return nil }
        return calendar.date(from: DateComponents(year: y, month: m, day: d))
    }

    /// yyyy-MM-dd for a moment, in `calendar`'s zone.
    static func iso(_ date: Date, calendar: Calendar) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }
}

// MARK: - The hero card

/// What Today's hero card shows: the first day in the week with a leg that
/// hasn't ended, every leg of it (C6), and how to say when.
struct StaffHero: Equatable {
    let day: StaffWeekDay
    /// "Today", "Tonight", "Tomorrow", "Thursday".
    let title: String
    /// The leg the relative line and Running late are about: the first
    /// one on the day that hasn't ended.
    let nextLeg: StaffShift
    /// "Starts in 2h 15m", "On now · until 10pm", "Tomorrow at 4pm".
    let relative: String
    /// Whether the next leg is today's and hasn't ended — Running late
    /// is for today's shift only (H1).
    let canRunLate: Bool
}

enum StaffTodayPlan {
    static func hero(week: [StaffWeekDay], now: Date, calendar: Calendar = .current) -> StaffHero? {
        for (index, day) in week.enumerated() {
            let legs = day.legs
            guard !legs.isEmpty else { continue }
            let open = legs.first { leg in
                guard let span = StaffTime.span(dayISO: day.date, start: leg.shiftStart, end: leg.shiftEnd,
                                                calendar: calendar) else { return true }
                return (span.end ?? span.start.addingTimeInterval(3600)) > now
            }
            guard let next = open else { continue }
            let span = StaffTime.span(dayISO: day.date, start: next.shiftStart, end: next.shiftEnd,
                                      calendar: calendar)
            // The server's own "today" is the restaurant's service date
            // (C5): a closer at 12:30am is still on last night's shift, so
            // the flag is trusted over the phone's calendar.
            let isToday = day.isToday
            let title: String
            if isToday {
                let startMinutes = next.shiftStart.flatMap(StaffTime.minutes) ?? 0
                title = startMinutes >= 16 * 60 ? "Tonight" : "Today"
            } else if index > 0 && week[index - 1].isToday {
                title = "Tomorrow"
            } else {
                title = day.weekday
            }
            let rel = relative(dayTitle: title, start: span?.start, startText: next.shiftStart,
                               endText: next.shiftEnd, now: now)
            let ended = span.map { ($0.end ?? $0.start) <= now } ?? false
            return StaffHero(day: day, title: title, nextLeg: next, relative: rel,
                             canRunLate: isToday && !ended && next.shiftStart != nil)
        }
        return nil
    }

    /// "Starts in 2h 15m" within 12 hours; "On now · until 10pm" once it
    /// has started; "Tomorrow at 4pm" / "Thursday at 4pm" further ahead.
    static func relative(dayTitle: String, start: Date?, startText: String?, endText: String?,
                         now: Date) -> String {
        let at = StaffTime.label(startText ?? "")
        guard let start else { return at.isEmpty ? dayTitle : "\(dayTitle) at \(at)" }
        if start <= now {
            let until = StaffTime.label(endText ?? "")
            return until.isEmpty ? "On now" : "On now \u{00B7} until \(until)"
        }
        let minutes = Int((start.timeIntervalSince(now) / 60).rounded(.up))
        if minutes <= 12 * 60 {
            if minutes < 60 { return "Starts in \(minutes)m" }
            let h = minutes / 60, m = minutes % 60
            return m == 0 ? "Starts in \(h)h" : "Starts in \(h)h \(m)m"
        }
        let name = (dayTitle == "Today" || dayTitle == "Tonight") ? "Today" : dayTitle
        return "\(name) at \(at)"
    }

    /// The shifts past the seven-day week (`upcoming` runs from today and
    /// goes past day 7), grouped by date — the "Later" rows (UX-10).
    static func later(upcoming: [StaffShift], week: [StaffWeekDay]) -> [(date: String, legs: [StaffShift])] {
        guard let last = week.last?.date else { return [] }
        var order: [String] = []
        var byDate: [String: [StaffShift]] = [:]
        for leg in upcoming {
            guard let iso = leg.dateISO ?? leg.date, iso.count >= 10, iso > last else { continue }
            if byDate[iso] == nil { order.append(iso) }
            byDate[iso, default: []].append(leg)
        }
        return order.sorted().map { ($0, byDate[$0] ?? []) }
    }

    /// The first shift past the week — "Next on 10/14/26 at 4pm" when the
    /// whole week is off.
    static func nextBeyondWeek(upcoming: [StaffShift], week: [StaffWeekDay]) -> String? {
        guard let first = later(upcoming: upcoming, week: week).first,
              let leg = first.legs.first else { return nil }
        let at = StaffTime.label(leg.shiftStart ?? "")
        return at.isEmpty ? "Next on \(CavnarDate.mdy(first.date))" : "Next on \(CavnarDate.mdy(first.date)) at \(at)"
    }

    /// The scheduled hours to show for "this week": the payroll week from
    /// /staff/api/stats when it answered, else the seven days' total.
    static func weekHours(stats: StaffStats?, shifts: StaffShiftsResponse?) -> (hours: Double, label: String)? {
        if let h = stats?.scheduled?.hours {
            return (h, "Hours this week")
        }
        if let h = shifts?.weekHours {
            return (h, "Hours, next 7 days")
        }
        return nil
    }

    /// The worked line under the hours tile: "18.5 worked through 9/30/26";
    /// nil when the POS can't say.
    static func workedLine(_ stats: StaffStats?) -> String? {
        guard let actual = stats?.actual, actual.available == true, let h = actual.hours else { return nil }
        if let asOf = actual.asOfLabel, !asOf.isEmpty {
            return "\(StaffTime.figure(h)) worked through \(asOf)"
        }
        return "\(StaffTime.figure(h)) worked"
    }

    /// The day row's VoiceOver sentence: "Monday 9/29/26, 4pm to 10pm,
    /// Server, 6 hours" — one stop per day (UX-18).
    static func accessibilityLabel(_ day: StaffWeekDay) -> String {
        var parts = [day.isToday ? "Today, \(day.weekday) \(CavnarDate.mdy(day.date))"
                                 : "\(day.weekday) \(CavnarDate.mdy(day.date))"]
        let legs = day.legs
        if legs.isEmpty {
            parts.append(day.isPosted ? "Off" : "Not posted yet")
        }
        for leg in legs {
            let s = StaffTime.label(leg.shiftStart ?? ""), e = StaffTime.label(leg.shiftEnd ?? "")
            if !s.isEmpty || !e.isEmpty { parts.append("\(s) to \(e)") }
            if let role = leg.role, !role.isEmpty { parts.append(role) }
            if let station = leg.station, !station.isEmpty { parts.append("on \(station)") }
            if let section = leg.section, !section.isEmpty { parts.append("\(section) section") }
            if let h = leg.hoursLabel { parts.append(h.replacingOccurrences(of: "hrs", with: "hours")
                                                        .replacingOccurrences(of: "hr", with: "hour")) }
            if let r = leg.request { parts.append(r.detail.trimmingCharacters(in: CharacterSet(charactersIn: "."))) }
            if let note = leg.noteLine { parts.append("Note: \(note)") }
        }
        return parts.joined(separator: ", ")
    }
}

// MARK: - Waiting on you

enum StaffWaiting {
    /// One sentence for the strip: the first ask or offer, and how many
    /// more. Nil when nothing waits.
    static func sentence(_ waiting: StaffWaitingResponse?) -> String? {
        guard let waiting, waiting.count > 0 else { return nil }
        let first: String
        if let ask = waiting.asks?.first {
            let theirs = when(ask.date, ask.shiftStart)
            let yours = when(ask.targetDate, ask.targetStart)
            first = "\(ask.employeeName ?? "A teammate") asked to swap their \(theirs) for your \(yours)."
        } else if let offer = waiting.offers?.first {
            let role = offer.role.map { " (\($0))" } ?? ""
            first = "You\u{2019}re offered \(when(offer.date, offer.shiftStart))\(role)."
        } else {
            return nil
        }
        let more = waiting.count - 1
        return more > 0 ? "\(first) And \(more) more." : first
    }

    /// "Wed 10/1/26 11am".
    static func when(_ iso: String?, _ start: String?) -> String {
        var parts: [String] = []
        var cal = Calendar(identifier: .gregorian)
        cal.locale = Locale(identifier: "en_US_POSIX")
        if let iso, let d = StaffTime.day(iso, calendar: cal) {
            let weekday = cal.shortWeekdaySymbols[cal.component(.weekday, from: d) - 1]
            parts.append("\(weekday) \(CavnarDate.mdy(iso))")
        } else if let iso {
            parts.append(CavnarDate.mdy(iso))
        }
        if let start, !start.isEmpty { parts.append(StaffTime.label(start)) }
        return parts.joined(separator: " ")
    }
}

// MARK: - Money (tips)

enum StaffMoney {
    /// "$186", "$186.50" — Space Grotesk at the call site.
    static func label(_ value: Double) -> String {
        let cents = (value * 100).rounded() / 100
        if cents == cents.rounded() { return "$\(Int(cents))" }
        return "$" + String(format: "%.2f", cents)
    }
}

// MARK: - Freshness (H6)

enum StaffFreshness {
    /// "Updated 3:42pm" after a live load; "As of 3:42pm · saved on this
    /// phone" while painting from StaffCache; a past day carries its date.
    static func stamp(at: Date?, fromCache: Bool, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard let at else { return nil }
        let sameDay = calendar.isDate(at, inSameDayAs: now)
        let when = sameDay ? CavnarDate.time(at, in: calendar.timeZone).replacingOccurrences(of: ":00", with: "")
                           : CavnarDate.mdyTime(at, in: calendar.timeZone).replacingOccurrences(of: ":00", with: "")
        return fromCache ? "As of \(when) \u{00B7} saved on this phone" : "Updated \(when)"
    }

    /// The amber line under Today's header while the week on screen is the
    /// phone's copy or a refresh failed (iOS readability round): "As of
    /// 3:42pm · offline", or "· couldn't refresh" when the phone is online
    /// but the read failed.
    static func warning(at: Date?, offline: Bool, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard let at else { return nil }
        let sameDay = calendar.isDate(at, inSameDayAs: now)
        let when = sameDay ? CavnarDate.time(at, in: calendar.timeZone).replacingOccurrences(of: ":00", with: "")
                           : CavnarDate.mdyTime(at, in: calendar.timeZone).replacingOccurrences(of: ":00", with: "")
        return "As of \(when) \u{00B7} \(offline ? "offline" : "couldn\u{2019}t refresh")"
    }
}

// MARK: - The hero's one primary (iOS readability round, 10/8/26)

/// The one primary the hero spends its ember on, by where the person is in
/// the day: on shift or within 30 minutes of the start → Tasks (with the
/// count done); before a shift today → Running late; otherwise none, and
/// every action stays quiet.
enum StaffHeroPrimary: Equatable {
    case tasks, late, none
}

extension StaffTodayPlan {
    /// How close "about to start" is: the half hour before a leg.
    static let tasksLeadMinutes = 30

    static func heroPrimary(_ hero: StaffHero, now: Date, hasTasks: Bool,
                            calendar: Calendar = .current) -> StaffHeroPrimary {
        guard hero.day.isToday else { return .none }
        let span = StaffTime.span(dayISO: hero.day.date, start: hero.nextLeg.shiftStart,
                                  end: hero.nextLeg.shiftEnd, calendar: calendar)
        let started = span.map { $0.start <= now } ?? false
        let soon = span.map { $0.start.timeIntervalSince(now) <= TimeInterval(tasksLeadMinutes * 60) } ?? false
        if (started || soon) && hasTasks { return .tasks }
        if hero.canRunLate && !started { return .late }
        return .none
    }

    /// One line for a day of the week: "4pm – 10pm · Server", a double as
    /// "11am – 3pm + 5pm – 10pm · Server"; "Off" / "Not posted yet".
    static func dayLine(_ day: StaffWeekDay) -> String {
        let legs = day.legs
        guard !legs.isEmpty else { return day.isPosted ? "Off" : "Not posted yet" }
        let times = legs.map { $0.timeRange.isEmpty ? "Time to be set" : $0.timeRange }
            .joined(separator: " + ")
        let role = legs.compactMap(\.role).first(where: { !$0.isEmpty })
        return [times, role].compactMap { $0 }.joined(separator: " \u{00B7} ")
    }

    /// Whether a day has more than its one line to show on a tap: a
    /// station, section, hours, break, note or a request on a leg.
    static func dayHasDetail(_ day: StaffWeekDay) -> Bool {
        day.legs.contains { leg in
            (leg.station?.isEmpty == false) || (leg.section?.isEmpty == false) || leg.hoursLabel != nil
                || leg.breakLine != nil || leg.noteLine != nil || leg.request != nil
        }
    }

    /// Coworkers on the hero's day, the ones whose hours overlap mine
    /// first (otherwise in the order the server sent).
    static func coworkersByOverlap(_ people: [StaffCoworker], legs: [StaffShift]) -> [StaffCoworker] {
        func range(_ start: String?, _ end: String?) -> (Int, Int)? {
            guard let s = start.flatMap(StaffTime.minutes) else { return nil }
            var e = end.flatMap(StaffTime.minutes) ?? (s + 60)
            if e <= s { e += 1440 }
            return (s, e)
        }
        let mine = legs.compactMap { range($0.shiftStart, $0.shiftEnd) }
        func overlaps(_ c: StaffCoworker) -> Bool {
            guard let r = range(c.shiftStart, c.shiftEnd) else { return false }
            return mine.contains { r.0 < $0.1 && $0.0 < r.1 }
        }
        return people.filter(overlaps) + people.filter { !overlaps($0) }
    }
}

// MARK: - From your manager (urgent and unread, on Today)

enum StaffManagerStrip {
    /// One sentence for Today's strip under the hero: urgent announcements
    /// not yet "Got it", and manager replies not yet read. Nil when neither
    /// waits — ordinary announcements stay behind the inbox tray.
    /// `opensThread` when only replies wait.
    static func content(_ badge: StaffInboxBadge?) -> (text: String, urgent: Bool, opensThread: Bool)? {
        guard let badge else { return nil }
        let urgent = badge.urgentUnread
        let replies = max(0, badge.unreadMessages ?? 0)
        var parts: [String] = []
        if let first = urgent.first {
            let title = (first.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            parts.append(urgent.count > 1 ? "\(urgent.count) urgent notes from your manager"
                                          : (title.isEmpty ? "An urgent note from your manager" : "Urgent: \(title)"))
        }
        if replies > 0 {
            parts.append(replies == 1 ? "1 new reply from your manager" : "\(replies) new replies from your manager")
        }
        guard !parts.isEmpty else { return nil }
        return (parts.joined(separator: " \u{00B7} "), !urgent.isEmpty, urgent.isEmpty)
    }
}
