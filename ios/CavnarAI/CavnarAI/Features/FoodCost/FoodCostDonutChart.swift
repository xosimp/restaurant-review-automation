import SwiftUI

/// One slice of a FoodCostDonutChart — waste_items and overstock items have
/// different underlying shapes (waste %, current/par stock) but both boil
/// down to "a name, a dollar value, and a short subtitle" for this chart.
/// subtitle is a pre-built Text (not a String) so the call site can give
/// its numeric portions Space Grotesk and its words Plus Jakarta Sans —
/// same mixed-font-via-Text-concatenation technique RoleDonutChart's own
/// legend already uses, matching this app's "digits are always Space
/// Grotesk" rule everywhere else.
struct FoodCostDonutSlice: Identifiable {
    let id: String
    let name: String
    let value: Double
    let subtitle: Text
}

/// Same ring-plus-legend construction as Labor's RoleDonutChart (segmented
/// Circle().trim() arcs, not Swift Charts — a pie/donut mark wasn't a great
/// fit for the same center-total-plus-legend layout that chart already
/// nailed), reusing its exact 12-color palette so a color always means the
/// same relative "how many things ahead of it" position across both
/// modules, and its exact collapse/expand-to-show-all pattern (was a
/// static, non-interactive "+N more" label — matched RoleDonutChart's own
/// real tap-to-expand instead). Deliberately unboxed — no card background/
/// border — the ring itself is already a strong enough visual anchor that
/// it doesn't need a frame around it too; a kicker label is what marks the
/// section start.
struct FoodCostDonutChart: View {
    let title: String
    let slices: [FoodCostDonutSlice]
    let centerLabel: String

    @State private var isExpanded = false

    private var total: Double { slices.reduce(0) { $0 + $1.value } }
    private static let ringSize: CGFloat = 92
    private static let collapseThreshold = 4

    private var visibleSlices: [FoodCostDonutSlice] {
        isExpanded ? slices : Array(slices.prefix(Self.collapseThreshold))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)

            if slices.isEmpty {
                Text("Nothing to show this week")
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
            } else {
                HStack(alignment: .center, spacing: 20) {
                    ring
                        .frame(width: Self.ringSize, height: Self.ringSize)
                    legend
                }
                if slices.count > Self.collapseThreshold {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.22)) { isExpanded.toggle() }
                    } label: {
                        HStack(spacing: 4) {
                            Text(isExpanded ? "Show less" : "Show all \(slices.count)")
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
    }

    private static let ringStrokeWidth: CGFloat = 13

    /// A stroked Circle renders half its line width OUTSIDE its own layout
    /// frame (SwiftUI doesn't clip shapes to their frame by default) — at
    /// 13pt this bled 6.5pt past the ring's nominal 92pt frame on every
    /// side, unclipped, which is exactly why the ring visually sat further
    /// toward the screen edge than the "TOP WASTE OFFENDERS"/"OVERSTOCKED"
    /// kicker directly above it, even though both share the same leading
    /// edge in layout terms. Insetting by half the stroke width keeps the
    /// stroke's rendered bleed inside the ring's own 92pt frame, so it
    /// lines up flush with the heading again.
    private var ring: some View {
        ZStack {
            Circle()
                .stroke(Color.cavnarPaper3.opacity(0.5), style: StrokeStyle(lineWidth: Self.ringStrokeWidth))
            ForEach(Array(slices.enumerated()), id: \.element.id) { index, _ in
                let (start, end) = segment(at: index)
                Circle()
                    .trim(from: start, to: end)
                    .stroke(color(at: index), style: StrokeStyle(lineWidth: Self.ringStrokeWidth, lineCap: .butt))
                    .rotationEffect(.degrees(-90))
                    .shadow(color: color(at: index).opacity(0.45), radius: 2)
            }
            VStack(spacing: 1) {
                Text(formattedTotal)
                    .font(.cavnarNumber(13, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text(centerLabel)
                    .font(.cavnarBody(7, weight: 700))
                    .tracking(0.8)
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(Self.ringStrokeWidth / 2)
    }

    private var legend: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(visibleSlices.enumerated()), id: \.element.id) { index, slice in
                HStack(alignment: .top, spacing: 7) {
                    Circle()
                        .fill(color(at: index))
                        .frame(width: 7, height: 7)
                        .padding(.top, 3)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(slice.name)
                            .font(.cavnarBody(11.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                            .lineLimit(1)
                        slice.subtitle
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    Spacer(minLength: 4)
                    Text("$\(Int(slice.value))")
                        .font(.cavnarNumber(11, weight: 700))
                        .foregroundStyle(color(at: index))
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func color(at index: Int) -> Color {
        RoleDonutChart.colors[index % RoleDonutChart.colors.count]
    }

    /// Segments are always computed against the FULL slice list, not the
    /// collapsed visibleSlices subset — the ring shows 100% of the total
    /// regardless of how many legend rows are currently visible, same
    /// reasoning as RoleDonutChart's own segment(at:).
    private func segment(at index: Int) -> (Double, Double) {
        // index normally always falls inside slices (ring's ForEach derives
        // it from slices.enumerated() directly), but this is the one spot
        // in the whole Food Cost module that subscripts an array by a raw
        // Int rather than going through a safe .first(where:)/id-based
        // lookup like everywhere else here — guarding it costs nothing and
        // removes it as a possible crash vector if slices and this call
        // ever do go out of sync (e.g. a stale closure from a view update
        // mid-flight).
        guard total > 0, !slices.isEmpty, slices.indices.contains(index) else { return (0, 0) }
        let gapFraction = 0.012
        var cumulative = 0.0
        for i in 0..<index { cumulative += slices[i].value / total }
        let start = min(cumulative, 1)
        let thisFraction = slices[index].value / total
        let end = min(start + max(thisFraction - gapFraction, 0), 1)
        return (start, end)
    }

    private var formattedTotal: String {
        if total >= 1_000_000 { return String(format: "$%.1fM", total / 1_000_000) }
        if total >= 1000 { return String(format: "$%.0fk", total / 1000) }
        return "$\(Int(total))"
    }
}
