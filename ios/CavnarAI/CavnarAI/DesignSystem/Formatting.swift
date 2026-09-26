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
        let t = time(date, in: timeZone)
        return t.isEmpty ? mdy(date, in: timeZone) : "\(mdy(date, in: timeZone)) · \(t)"
    }

    /// `6:45pm` — the time of day alone, lowercase am/pm, no seconds, no
    /// leading zero (DESIGN_SYSTEM → Dates and times). Not `h:mm a`
    /// ("6:45 PM"), which is the locale's form, not ours.
    static func time(_ date: Date, in timeZone: TimeZone = .current) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let c = calendar.dateComponents([.hour, .minute], from: date)
        guard let h = c.hour, let m = c.minute else { return "" }
        let hour12 = h % 12 == 0 ? 12 : h % 12
        return "\(hour12):\(String(format: "%02d", m))\(h < 12 ? "am" : "pm")"
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

    /// M/D/YY for a server TIMESTAMP, on the phone's own calendar day. The
    /// ledger's stamps (`first_shown_at`, `answered_at`, …) are SQLite's
    /// `datetime('now')` — UTC — so `2026-08-02 03:10:00` is 8/1/26 in
    /// Chicago; `mdy(_:)` on the string read the UTC day. A bare date has
    /// no time to shift and reads as given, as does anything unparseable.
    static func mdyLocal(_ stamp: String, in timeZone: TimeZone = .current) -> String {
        guard let date = timestamp(stamp) else { return mdy(stamp) }
        return mdy(date, in: timeZone)
    }

    /// A server timestamp as a moment: `yyyy-MM-dd HH:mm[:ss[.fff]]`, with
    /// a space or a `T`, read as UTC unless it carries `Z` or an offset
    /// (`+00:00`, `-0500`). Nil for a bare date or anything else.
    static func timestamp(_ stamp: String) -> Date? {
        let c = Array(stamp.trimmingCharacters(in: .whitespaces))
        func int(_ from: Int, _ to: Int) -> Int? {
            guard to <= c.count, c[from..<to].allSatisfy(\.isNumber) else { return nil }
            return Int(String(c[from..<to]))
        }
        guard c.count >= 16, let y = int(0, 4), c[4] == "-", let mo = int(5, 7), c[7] == "-",
              let d = int(8, 10), c[10] == " " || c[10] == "T",
              let h = int(11, 13), c[13] == ":", let mi = int(14, 16) else { return nil }
        var i = 16
        var seconds = 0.0
        if i < c.count, c[i] == ":" {
            guard let s = int(i + 1, i + 3) else { return nil }
            seconds = Double(s)
            i += 3
            if i < c.count, c[i] == "." {
                var j = i + 1
                while j < c.count, c[j].isNumber { j += 1 }
                seconds += Double("0" + String(c[i..<j])) ?? 0
                i = j
            }
        }
        var offset = 0
        if i < c.count {
            let rest = String(c[i...])
            if rest != "Z" {
                guard let sign = rest.first, sign == "+" || sign == "-" else { return nil }
                let digits = rest.dropFirst().replacingOccurrences(of: ":", with: "")
                guard digits.count == 4 || digits.count == 2, digits.allSatisfy(\.isNumber),
                      let v = Int(digits) else { return nil }
                let hh = digits.count == 4 ? v / 100 : v
                let mm = digits.count == 4 ? v % 100 : 0
                offset = (hh * 3600 + mm * 60) * (sign == "-" ? -1 : 1)
            }
        }
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = TimeZone(secondsFromGMT: 0)!
        guard let base = utc.date(from: DateComponents(year: y, month: mo, day: d, hour: h, minute: mi)) else {
            return nil
        }
        return base.addingTimeInterval(seconds - Double(offset))
    }

    /// `2026-09-24` — `date`'s calendar day on `timeZone`, for comparing
    /// against a server date string.
    static func isoDay(_ date: Date, in timeZone: TimeZone = .current) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
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
