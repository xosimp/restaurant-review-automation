import SwiftUI

/// Colorful ring/donut chart for a role-by-role labor cost breakdown — same
/// visual language as the web dashboard's "Labor Cost by Role" donut
/// (colored arc segments around a ring, center total, legend rows with
/// name/%/hours/headcount/$), rebuilt with SwiftUI Shapes in place of raw
/// SVG. Replaces the old plain vertical list, which just grew a flat text
/// row per role with no visual weighting of which roles actually drive cost.
struct RoleDonutChart: View {
    let roles: [LaborRoleSummary]

    /// Same 7-color sequence the web role donut cycles through, so the two
    /// surfaces read as the same chart.
    static let colors: [Color] = [
        Color(red: 0.784, green: 0.294, blue: 0.184),  // #c84b2f
        Color(red: 0.435, green: 0.812, blue: 0.592),  // #6fcf97
        Color(red: 0.937, green: 0.624, blue: 0.153),  // #ef9f27
        Color(red: 0.376, green: 0.678, blue: 0.961),  // #60adf5
        Color(red: 0.702, green: 0.616, blue: 0.953),  // #b39df3
        Color(red: 0.957, green: 0.447, blue: 0.714),  // #f472b6
        Color(red: 0.302, green: 0.816, blue: 0.882),  // #4dd0e1
    ]

    private var totalCost: Double { roles.reduce(0) { $0 + $1.laborCost } }

    // Bigger than the original 108pt, and shared by both sides of the row
    // (the ring is fixed to it, the legend gets a matching minHeight) so the
    // donut and its legend read as one balanced pairing instead of a small
    // chart floating next to a taller stack of text.
    private static let ringSize: CGFloat = 148

    var body: some View {
        HStack(alignment: .center, spacing: 22) {
            ring
                .frame(width: Self.ringSize, height: Self.ringSize)
            legend
                .frame(minHeight: Self.ringSize)
        }
    }

    private var ring: some View {
        ZStack {
            Circle()
                .stroke(Color.cavnarPaper3.opacity(0.5), style: StrokeStyle(lineWidth: 16))
            ForEach(Array(roles.enumerated()), id: \.element.id) { index, _ in
                let (start, end) = segment(at: index)
                Circle()
                    .trim(from: start, to: end)
                    .stroke(color(at: index), style: StrokeStyle(lineWidth: 16, lineCap: .butt))
                    .rotationEffect(.degrees(-90))
                    .shadow(color: color(at: index).opacity(0.5), radius: 3)
            }
            VStack(spacing: 2) {
                Text(formattedTotal)
                    .font(.cavnarNumber(19, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text("TOTAL")
                    .font(.cavnarBody(9, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    private var legend: some View {
        VStack(alignment: .leading, spacing: 0) {
            Spacer(minLength: 0)
            VStack(alignment: .leading, spacing: 12) {
                ForEach(Array(roles.enumerated()), id: \.element.id) { index, role in
                    HStack(alignment: .top, spacing: 8) {
                        Circle()
                            .fill(color(at: index))
                            .frame(width: 8, height: 8)
                            .padding(.top, 4)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(role.role)
                                .font(.cavnarBody(12, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                                .lineLimit(1)
                            // Digits in Space Grotesk (cavnarNumber), same
                            // as every other numeric value in the app —
                            // this line was plain body text throughout,
                            // including the hours/headcount/cost figures.
                            (Text(formattedHours(role.hours)).font(.cavnarNumber(9))
                                + Text("h · ").font(.cavnarBody(9))
                                + Text("\(role.headcount)").font(.cavnarNumber(9))
                                + Text(" staff · $").font(.cavnarBody(9))
                                + Text(formattedComma(role.laborCost)).font(.cavnarNumber(9)))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer(minLength: 4)
                        Text(String(format: "%.0f%%", role.laborPct))
                            .font(.cavnarNumber(13, weight: 700))
                            .foregroundStyle(color(at: index))
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func color(at index: Int) -> Color {
        Self.colors[index % Self.colors.count]
    }

    /// Matches the web chart's stroke-dasharray gap between segments — a
    /// thin sliver of visual separation so adjacent same-hue-family
    /// segments (there are only 7 colors, cycling for >7 roles) don't blur
    /// into one continuous ring.
    private func segment(at index: Int) -> (Double, Double) {
        guard totalCost > 0, !roles.isEmpty else { return (0, 0) }
        let gapFraction = 0.01
        var cumulative = 0.0
        for i in 0..<index { cumulative += roles[i].laborCost / totalCost }
        let start = min(cumulative, 1)
        let thisFraction = roles[index].laborCost / totalCost
        let end = min(start + max(thisFraction - gapFraction, 0), 1)
        return (start, end)
    }

    private var formattedTotal: String {
        if totalCost >= 1_000_000 { return String(format: "$%.1fM", totalCost / 1_000_000) }
        if totalCost >= 1000 { return String(format: "$%.0fk", totalCost / 1000) }
        return "$\(Int(totalCost))"
    }

    private func formattedHours(_ hours: Double) -> String {
        hours.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(hours)) : String(format: "%.1f", hours)
    }

    private func formattedComma(_ value: Double) -> String {
        let intVal = Int(value.rounded())
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        return formatter.string(from: NSNumber(value: intVal)) ?? "\(intVal)"
    }
}
