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
