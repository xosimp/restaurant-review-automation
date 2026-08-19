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
