import SwiftUI

/// Labor Analytics tab — savings breakdown, industry benchmark, and a real
/// performance chart (trend/by-day toggle), matching the web dashboard's
/// Labor "Analytics" sub-tab. Takes the same `LaborStats` the Overview tab
/// already fetched (LaborViewModel) rather than re-fetching — the AI
/// insight card that used to live here moved to Overview to match the web
/// tab's own placement (see LaborView.swift).
struct LaborAnalyticsSection: View {
    @Bindable var viewModel: LaborAnalyticsViewModel
    let laborStats: LaborStats?

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if let stats = laborStats {
                savingsTiles(stats)
                benchmarkBar(stats)
                LaborPerformanceChart(
                    trend: viewModel.trend, dowSummary: stats.dowSummary, target: stats.target,
                    hasPlayedIntro: $viewModel.hasPlayedBarIntro
                )
            } else if viewModel.isLoading {
                ProgressView().frame(maxWidth: .infinity).padding(.vertical, 20)
            }
        }
    }

    @ViewBuilder
    private func savingsTiles(_ stats: LaborStats) -> some View {
        let b = stats.savingsBreakdown
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 10)], spacing: 10) {
            if b.laborMonthly > 0 {
                SavingsTile(value: formattedDollarsK(b.laborMonthly), label: "Monthly savings", sublabel: "if schedule optimized")
            } else {
                SavingsTile(value: formattedDollarsK(b.laborVsIndustryMonthly), label: "Saving vs. industry avg", sublabel: "per month vs 32% avg")
            }
            if b.laborAnnual > 0 {
                SavingsTile(value: formattedDollarsK(b.laborAnnual), label: "Annual savings", sublabel: "extrapolated yearly")
            } else {
                SavingsTile(value: formattedDollarsK(b.laborVsIndustryAnnual), label: "Annual advantage", sublabel: "vs. 32% industry avg/yr")
            }
            if b.laborOvertime > 0 {
                SavingsTile(value: formattedDollarsK(b.laborOvertime), label: "Overtime premium", sublabel: "0.5× rate on hours over 40", tone: Color.cavnarRed)
            }
            SavingsTile(
                value: "\(stats.overstaffedDays.count)",
                label: "Overstaffed days",
                sublabel: "vs \(stats.understaffedDays.count) understaffed",
                tone: stats.overstaffedDays.isEmpty ? Color.cavnarInk3 : Color.cavnarAmber
            )
        }
    }

    private func formattedDollarsK(_ value: Double) -> String {
        if value >= 1_000_000 { return String(format: "$%.1fM", value / 1_000_000) }
        if value >= 1000 { return String(format: "$%.0fk", value / 1000) }
        return "$\(Int(value))"
    }

    @ViewBuilder
    private func benchmarkBar(_ stats: LaborStats) -> some View {
        let pct = stats.overallLaborPct
        let target = max(stats.target, 1)
        let bucket = benchmarkBucket(pct: pct, target: target)
        let barFill = min(pct / 50 * 100, 100)
        let targetPos = min(target / 50 * 100, 97)

        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Labor % vs industry benchmark")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(0.4)
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                Text(String(format: "%.1f%%", pct))
                    .font(.cavnarNumber(13, weight: 700))
                    .foregroundStyle(bucket.color)
                Text(bucket.label)
                    .font(.cavnarBody(9, weight: 700))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(bucket.color)
                    .clipShape(Capsule())
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                    Capsule().fill(bucket.color)
                        .frame(width: geo.size.width * barFill / 100)
                    Rectangle().fill(Color.cavnarGreen)
                        .frame(width: 2)
                        .offset(x: geo.size.width * targetPos / 100)
                }
            }
            .frame(height: 10)
            Text("Industry range: 33–36% for full-service restaurants")
                .font(.cavnarBody(10))
                .foregroundStyle(Color.cavnarInk3)
        }
        .cavnarCard()
    }

    private func benchmarkBucket(pct: Double, target: Double) -> (label: String, color: Color) {
        if pct <= target - 3 { return ("Excellent", Color.cavnarGreen) }
        if pct <= target { return ("On Target", Color.cavnarGreen) }
        if pct <= target + 3 { return ("Slightly Over", Color.cavnarAmber) }
        if pct <= target + 8 { return ("Above Target", Color.cavnarAmber) }
        return ("Needs Attention", Color.cavnarRed)
    }
}

private struct SavingsTile: View {
    let value: String
    let label: String
    let sublabel: String
    var tone: Color = Color.cavnarGreen

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(value)
                .font(.cavnarNumber(22, weight: 700))
                .foregroundStyle(tone)
                .cavnarNumberGlow(tone)
            Text(label)
                .font(.cavnarBody(9, weight: 700))
                .tracking(0.6)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarInk3)
            Text(sublabel)
                .font(.cavnarBody(9))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .cavnarCard()
    }
}
