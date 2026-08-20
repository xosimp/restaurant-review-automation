import SwiftUI

/// One slice of a FoodCostDonutChart — waste_items and overstock items have
/// different underlying shapes (waste %, current/par stock) but both boil
/// down to "a name, a dollar value, and a short subtitle" for this chart.
struct FoodCostDonutSlice: Identifiable {
    let id: String
    let name: String
    let value: Double
    let subtitle: String
}

/// Same ring-plus-legend construction as Labor's RoleDonutChart (segmented
/// Circle().trim() arcs, not Swift Charts — a pie/donut mark wasn't a great
/// fit for the same center-total-plus-legend layout that chart already
/// nailed), reusing its exact 12-color palette so a color always means the
/// same relative "how many things ahead of it" position across both
/// modules. Deliberately unboxed — no card background/border — the ring
/// itself is already a strong enough visual anchor that it doesn't need a
/// frame around it too; a kicker label is what marks the section start.
struct FoodCostDonutChart: View {
    let title: String
    let slices: [FoodCostDonutSlice]
    let centerLabel: String

    private var total: Double { slices.reduce(0) { $0 + $1.value } }
    private static let ringSize: CGFloat = 92

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
            }
        }
    }

    private var ring: some View {
        ZStack {
            Circle()
                .stroke(Color.cavnarPaper3.opacity(0.5), style: StrokeStyle(lineWidth: 13))
            ForEach(Array(slices.enumerated()), id: \.element.id) { index, _ in
                let (start, end) = segment(at: index)
                Circle()
                    .trim(from: start, to: end)
                    .stroke(color(at: index), style: StrokeStyle(lineWidth: 13, lineCap: .butt))
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
    }

    private var legend: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(slices.prefix(4).enumerated()), id: \.element.id) { index, slice in
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
                        Text(slice.subtitle)
                            .font(.cavnarBody(9))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    Spacer(minLength: 4)
                    Text("$\(Int(slice.value))")
                        .font(.cavnarNumber(11, weight: 700))
                        .foregroundStyle(color(at: index))
                }
            }
            if slices.count > 4 {
                Text("+\(slices.count - 4) more")
                    .font(.cavnarBody(10, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.leading, 14)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func color(at index: Int) -> Color {
        RoleDonutChart.colors[index % RoleDonutChart.colors.count]
    }

    private func segment(at index: Int) -> (Double, Double) {
        guard total > 0, !slices.isEmpty else { return (0, 0) }
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
