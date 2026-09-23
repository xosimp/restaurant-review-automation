import Foundation

extension Double {
    /// Rounds to the nearest whole number and adds thousands separators —
    /// "1314" -> "1,314", "19388" -> "19,388". Same NumberFormatter
    /// approach RoleDonutChart's own formattedComma already used locally;
    /// pulled out here once a second/third call site needed the same
    /// thing (the PAR hours check banners on LaborView and
    /// ScheduleHistoryDetailView) rather than copy-pasting it again.
    var commaFormatted: String {
        let intVal = Int(self.rounded())
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        return formatter.string(from: NSNumber(value: intVal)) ?? "\(intVal)"
    }
}

/// Dates an owner reads are M/D/YY with no leading zeros — `9/21/26` —
/// everywhere (DESIGN_SYSTEM.md → Dates and times). Takes the ISO string
/// the API sends (`2026-09-21` or `2026-09-21 18:45:00`) and hands back
/// the owner-facing form; anything it cannot parse is returned as given
/// rather than blanked.
enum CavnarDate {
    static func mdy(_ iso: String) -> String {
        let p = iso.prefix(10).split(separator: "-")
        guard p.count == 3, let m = Int(p[1]), let d = Int(p[2]) else { return iso }
        return "\(m)/\(d)/\(p[0].suffix(2))"
    }

    /// `9/14/26 – 9/20/26`, collapsing to one date when both ends match.
    static func mdyRange(_ a: String, _ b: String) -> String {
        let s = mdy(a), e = mdy(b)
        return s == e ? s : "\(s) – \(e)"
    }

    /// The same form for a Date, read on `timeZone`'s calendar day — the
    /// phone's by default, `RestaurantClock.timeZone` for anything that
    /// happened at the restaurant.
    static func mdy(_ date: Date, in timeZone: TimeZone = .current) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        guard let y = c.year, let m = c.month, let d = c.day else { return "" }
        return "\(m)/\(d)/\(String(format: "%02d", y % 100))"
    }

    /// `9/21/26 · 6:45pm` for a Date, read on `timeZone`.
    static func mdyTime(_ date: Date, in timeZone: TimeZone = .current) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let c = calendar.dateComponents([.hour, .minute], from: date)
        guard let h = c.hour, let m = c.minute else { return mdy(date, in: timeZone) }
        let hour12 = h % 12 == 0 ? 12 : h % 12
        return "\(mdy(date, in: timeZone)) · \(hour12):\(String(format: "%02d", m))\(h < 12 ? "am" : "pm")"
    }

    /// `9/21/26 · 6:45pm` from `2026-09-21 18:45:00` (or `T18:45`); the
    /// date alone when there is no time to show.
    static func mdyTime(_ iso: String) -> String {
        let date = mdy(iso)
        guard iso.count >= 16 else { return date }
        let timePart = iso.dropFirst(11).prefix(5)
        let hm = timePart.split(separator: ":")
        guard hm.count == 2, let h = Int(hm[0]), let m = Int(hm[1]) else { return date }
        let hour12 = h % 12 == 0 ? 12 : h % 12
        return "\(date) · \(hour12):\(String(format: "%02d", m))\(h < 12 ? "am" : "pm")"
    }
}

extension NumberFormatter {
    /// Parses a decimal the way the keyboard in front of the user types it.
    /// `Double("3,50")` is nil in every comma-decimal locale, and a quick
    /// count that fell back to 0 on a nil stored the zero as a real price.
    static let cavnarDecimal: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.locale = .current
        return f
    }()
}
