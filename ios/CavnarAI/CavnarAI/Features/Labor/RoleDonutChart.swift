import SwiftUI

/// Colorful ring/donut chart for a role-by-role labor cost breakdown — same
/// visual language as the web dashboard's "Labor Cost by Role" donut
/// (colored arc segments around a ring, center total, legend rows with
/// name/%/hours/headcount/$), rebuilt with SwiftUI Shapes in place of raw
/// SVG. Replaces the old plain vertical list, which just grew a flat text
/// row per role with no visual weighting of which roles actually drive cost.
struct RoleDonutChart: View {
    let roles: [LaborRoleSummary]
    @Binding var isExpanded: Bool
    // Optional: labels the center total with the actual span it covers
    // (e.g. "2-WK TOTAL") instead of a bare "TOTAL" that reads as if it
    // matched a single week's PAR hours budget elsewhere on this screen.
    // roles/totalCost here are the restaurant's real historical shift
    // data — whatever's currently loaded, which for a fresh account can
    // be several weeks — not the one week a freshly generated schedule's
    // PAR banner budgets for; those two are different numbers by design,
    // not a bug, but "TOTAL" alone didn't say which one this was.
    var dateRange: LaborDateRange? = nil

    /// Started as the web role donut's 7-color cycle, but a real roster now
    /// runs up to 11 distinct roles (cook split into 4 stations, Carry Out,
    /// Shift Supervisor, Runner...) — cycling 7 colors across 11 roles put
    /// visually-identical colors on unrelated roles. Extended to 12 chosen
    /// for hue separation, not just appended: each new color sits in a gap
    /// the original 7 left empty (deep indigo, burnt amber, deep emerald,
    /// deep rose, slate) rather than being a lighter/darker twin of an
    /// existing one.
    static let colors: [Color] = [
        Color(red: 0.784, green: 0.294, blue: 0.184),  // #c84b2f ember red-orange
        Color(red: 0.435, green: 0.812, blue: 0.592),  // #6fcf97 mint green
        Color(red: 0.937, green: 0.624, blue: 0.153),  // #ef9f27 amber
        Color(red: 0.376, green: 0.678, blue: 0.961),  // #60adf5 sky blue
        Color(red: 0.702, green: 0.616, blue: 0.953),  // #b39df3 lavender
        Color(red: 0.957, green: 0.447, blue: 0.714),  // #f472b6 pink
        Color(red: 0.302, green: 0.816, blue: 0.882),  // #4dd0e1 cyan
        Color(red: 0.263, green: 0.220, blue: 0.792),  // #4338ca deep indigo
        Color(red: 0.851, green: 0.467, blue: 0.024),  // #d97706 burnt amber
        Color(red: 0.020, green: 0.588, blue: 0.412),  // #059669 deep emerald
        Color(red: 0.859, green: 0.153, blue: 0.467),  // #db2777 deep rose
        Color(red: 0.392, green: 0.455, blue: 0.545),  // #64748b slate
    ]

    /// Sorted highest-cost-first, once, so the ring's biggest segment leads
    /// and the collapsed legend's top rows are the roles that actually
    /// matter most to the number in the center.
    private var sortedRoles: [LaborRoleSummary] {
        roles.sorted { $0.laborCost > $1.laborCost }
    }

    // Past this many roles, the plain vertical list stopped being a legend
    // and became a scroll-filling wall of rows (11 roles, one per cook
    // station plus every FOH position) — collapse to the roles that
    // actually drive the total, with the rest a tap away.
    private static let collapseThreshold = 6

    private var visibleRoles: [LaborRoleSummary] {
        isExpanded ? sortedRoles : Array(sortedRoles.prefix(Self.collapseThreshold))
    }

    private var totalCost: Double { roles.reduce(0) { $0 + $1.laborCost } }

    private static let isoDayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private var totalLabel: String {
        guard let start = dateRange?.start, let end = dateRange?.end,
              let startDate = Self.isoDayFormatter.date(from: start),
              let endDate = Self.isoDayFormatter.date(from: end) else { return "TOTAL" }
        let days = Calendar.current.dateComponents([.day], from: startDate, to: endDate).day.map { $0 + 1 } ?? 0
        if days <= 0 { return "TOTAL" }
        if days % 7 == 0 { return "\(days / 7)-WK TOTAL" }
        return "\(days)-DAY TOTAL"
    }

    // Bigger than the original 108pt, and shared by both sides of the row
    // (the ring is fixed to it, the legend gets a matching minHeight) so the
    // donut and its legend read as one balanced pairing instead of a small
    // chart floating next to a taller stack of text.
    private static let ringSize: CGFloat = 148

    // Hand-computed height of exactly the collapsed (6-role) legend block,
    // not measured live — a hidden-view GeometryReader/PreferenceKey
    // measurement was tried first and didn't actually move the ring
    // (reported still top-aligned after that fix), so this replaces it
    // with arithmetic built from typography this view fully owns: each
    // row is a 12pt name line + 1pt inner spacing + a 9pt subtext line
    // (≈27pt for the two together, using this custom font family's
    // typical ~1.2x line-height), with 12pt between rows.
    private static let collapsedRowHeight: CGFloat = 27
    private static var collapsedLegendHeight: CGFloat {
        let rows = CGFloat(collapseThreshold)
        return rows * collapsedRowHeight + (rows - 1) * 12
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 22) {
                ring
                    .frame(width: Self.ringSize, height: Self.ringSize)
                    // Shifts the ring down by half the gap between its own
                    // height and the (fixed, always-6-row) legend height,
                    // landing its center on the legend's center regardless
                    // of how many rows are actually showing — expanding to
                    // "Show all N roles" grows the legend below this
                    // reference point without moving it.
                    .offset(y: max(0, (Self.collapsedLegendHeight - Self.ringSize) / 2))
                legend
                    .frame(minHeight: Self.ringSize)
            }
            if sortedRoles.count > Self.collapseThreshold {
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.22)) { isExpanded.toggle() }
                } label: {
                    HStack(spacing: 4) {
                        Text(isExpanded ? "Show less" : "Show all \(sortedRoles.count) roles")
                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .font(.system(size: 9, weight: .semibold))
                    }
                    .font(.cavnarBody(11, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private static let ringStrokeWidth: CGFloat = 16

    /// A stroked Circle renders half its line width OUTSIDE its own layout
    /// frame (SwiftUI doesn't clip shapes to their frame by default) — at
    /// 16pt this bled 8pt past the ring's nominal 148pt frame on every
    /// side, unclipped, visually shifting the ring away from whatever sits
    /// flush against the same leading edge above it. Insetting by half the
    /// stroke width keeps the stroke's rendered bleed inside the ring's
    /// own frame. Mirrors the identical fix in Food Cost's own donut
    /// (FoodCostDonutChart's `ring`), which shares this exact construction.
    private var ring: some View {
        ZStack {
            Circle()
                .stroke(Color.cavnarPaper3.opacity(0.5), style: StrokeStyle(lineWidth: Self.ringStrokeWidth))
            ForEach(Array(sortedRoles.enumerated()), id: \.element.id) { index, _ in
                let (start, end) = segment(at: index)
                Circle()
                    .trim(from: start, to: end)
                    .stroke(color(at: index), style: StrokeStyle(lineWidth: Self.ringStrokeWidth, lineCap: .butt))
                    .rotationEffect(.degrees(-90))
                    .shadow(color: color(at: index).opacity(0.5), radius: 3)
            }
            VStack(spacing: 2) {
                Text(formattedTotal)
                    .font(.cavnarNumber(19, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text(totalLabel)
                    .font(.cavnarBody(9, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(Self.ringStrokeWidth / 2)
    }

    private var legend: some View {
        legendRows(visibleRoles)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func legendRows(_ roles: [LaborRoleSummary]) -> some View {
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
                            + Text(role.laborCost.commaFormatted).font(.cavnarNumber(9)))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    Spacer(minLength: 4)
                    Text(String(format: "%.0f%%", role.laborPct))
                        .font(.cavnarNumber(13, weight: 700))
                        .foregroundStyle(color(at: index))
                }
            }
        }
    }

    private func color(at index: Int) -> Color {
        Self.colors[index % Self.colors.count]
    }

    /// Matches the web chart's stroke-dasharray gap between segments — a
    /// thin sliver of visual separation so adjacent segments don't blur
    /// into one continuous ring. Always computed against the full sorted
    /// list (not the collapsed legend subset) — the ring shows 100% of
    /// cost regardless of how many legend rows are currently visible.
    private func segment(at index: Int) -> (Double, Double) {
        guard totalCost > 0, !sortedRoles.isEmpty else { return (0, 0) }
        let gapFraction = 0.01
        var cumulative = 0.0
        for i in 0..<index { cumulative += sortedRoles[i].laborCost / totalCost }
        let start = min(cumulative, 1)
        let thisFraction = sortedRoles[index].laborCost / totalCost
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
}
