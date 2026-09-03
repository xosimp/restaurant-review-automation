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
                    dateRangeStart: stats.dateRange.start, dateRangeEnd: stats.dateRange.end,
                    hasPlayedIntro: viewModel.hasPlayedBarIntro,
                    onIntroPlayed: { viewModel.markBarIntroPlayed() }
                )
            } else if viewModel.isLoading {
                CavnarWorkingLine().padding(.vertical, 20)
            }
        }
    }

    @ViewBuilder
    private func savingsTiles(_ stats: LaborStats) -> some View {
        let b = stats.savingsBreakdown
        // Captured once per render, before the .onAppear below flips the
        // flag — every tile in this pass sees the same snapshot, so all
        // four count up together on first load instead of racing each
        // other for which gets to be "first" and flip the shared flag.
        let startFromZero = !viewModel.hasPlayedTilesIntro
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 10)], spacing: 10) {
            if b.laborMonthly > 0 {
                SavingsTile(numericValue: b.laborMonthly, format: formattedDollarsK, label: "Monthly savings", sublabel: "if schedule optimized", startFromZero: startFromZero)
            } else {
                SavingsTile(numericValue: b.laborVsIndustryMonthly, format: formattedDollarsK, label: "Saving vs. industry avg", sublabel: "per month vs 34.5% avg", startFromZero: startFromZero)
            }
            if b.laborAnnual > 0 {
                SavingsTile(numericValue: b.laborAnnual, format: formattedDollarsK, label: "Annual savings", sublabel: "extrapolated yearly", startFromZero: startFromZero)
            } else {
                SavingsTile(numericValue: b.laborVsIndustryAnnual, format: formattedDollarsK, label: "Annual advantage", sublabel: "vs. 34.5% industry avg/yr", startFromZero: startFromZero)
            }
            if b.laborOvertime > 0 {
                SavingsTile(numericValue: b.laborOvertime, format: formattedDollarsK, label: "Overtime premium", sublabel: "0.5× rate on hours over 40", tone: Color.cavnarRed, startFromZero: startFromZero)
            }
            SavingsTile(
                numericValue: Double(stats.overstaffedDays.count),
                format: { "\(Int($0.rounded()))" },
                label: "Overstaffed days",
                sublabel: "vs \(stats.understaffedDays.count) understaffed",
                tone: stats.overstaffedDays.isEmpty ? Color.cavnarInk3 : Color.cavnarAmber,
                startFromZero: startFromZero
            )
        }
        .onAppear { viewModel.markTilesIntroPlayed() }
    }

    private func formattedDollarsK(_ value: Double) -> String {
        if value >= 1_000_000 { return String(format: "$%.1fM", value / 1_000_000) }
        if value >= 1000 { return String(format: "$%.0fk", value / 1000) }
        return "$\(Int(value))"
    }

    // National Restaurant Association 2024 full-service median range — see
    // labor.py's own comment for the same figure and its source. Kept here
    // as the single place the benchmark BAND's position is computed; the
    // "Industry range: 33–36%" caption text below is the same numbers
    // spelled out, not a second, independently-maintained source of truth.
    private static let industryLow = 33.0
    private static let industryHigh = 36.0

    @ViewBuilder
    private func benchmarkBar(_ stats: LaborStats) -> some View {
        let pct = stats.overallLaborPct
        let target = max(stats.target, 1)
        let bucket = benchmarkBucket(pct: pct, target: target)
        let barFill = min(pct / 50 * 100, 100)
        let targetPos = min(target / 50 * 100, 97)
        let industryStart = min(Self.industryLow / 50 * 100, 100)
        let industryWidth = max(0, min((Self.industryHigh - Self.industryLow) / 50 * 100, 100 - industryStart))

        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Labor % vs industry benchmark")
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(0.4)
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                Text(String(format: "%.1f%%", pct))
                    .font(.cavnarNumber(14.5, weight: 700))
                    .foregroundStyle(bucket.color)
                Text(bucket.label)
                    .font(.cavnarBody(13.5, weight: 700))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(bucket.color)
                    .clipShape(Capsule())
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                    // The industry range is a band (33–36%), not a single
                    // point, so it gets a shaded region rather than a tick
                    // — a single line would falsely imply one exact
                    // "average" value instead of the real reported range.
                    Rectangle().fill(Color.cavnarInk.opacity(0.35))
                        .frame(width: geo.size.width * industryWidth / 100)
                        .offset(x: geo.size.width * industryStart / 100)
                    Capsule().fill(bucket.color)
                        .frame(width: geo.size.width * barFill / 100)
                    Rectangle().fill(Color.cavnarGreen)
                        .frame(width: 2)
                        .offset(x: geo.size.width * targetPos / 100)
                }
            }
            .frame(height: 10)
            HStack(spacing: 14) {
                HStack(spacing: 4) {
                    Rectangle().fill(Color.cavnarGreen).frame(width: 8, height: 2)
                    Text("Your target (\(Int(target))%)")
                }
                HStack(spacing: 4) {
                    Rectangle().fill(Color.cavnarInk.opacity(0.35)).frame(width: 8, height: 8)
                    Text("Industry range: 33–36% for full-service restaurants")
                }
            }
            .font(.cavnarBody(14))
            .foregroundStyle(Color.cavnarInk3)

            // Measured against the industry band's midpoint (34.5%), not
            // the restaurant's own target — "your target" and "industry
            // average" are two different lines on this same bar, and this
            // sentence is specifically about the second one.
            let industryMid = (Self.industryLow + Self.industryHigh) / 2
            let diff = pct - industryMid
            let isBelow = diff <= 0
            (Text("Your restaurant is ")
                + Text(String(format: "%.1f%%", abs(diff))).font(.cavnarNumber(14, weight: 700))
                + Text(isBelow ? " below" : " above")
                + Text(" other similar restaurants in the U.S."))
                .font(.cavnarBody(14))
                .foregroundStyle(isBelow ? Color.cavnarGreen : Color.cavnarRed)
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
    let numericValue: Double
    let format: (Double) -> String
    let label: String
    let sublabel: String
    var tone: Color = Color.cavnarGreen
    let startFromZero: Bool

    // Same count-up-once treatment as Home's ValueChartCard hero number —
    // a flat instant figure reads as "just a stat," counting up reads as
    // "watch how much this is."
    @State private var animatedValue: Double = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            AnimatableTileNumber(value: animatedValue, format: format)
                .font(.cavnarNumber(22, weight: 700))
                .foregroundStyle(tone)
                .cavnarNumberGlow(tone)
                .onAppear {
                    if startFromZero {
                        withAnimation(.easeOut(duration: 1.2)) { animatedValue = numericValue }
                    } else {
                        animatedValue = numericValue
                    }
                }
                .onChange(of: numericValue) { _, newValue in
                    animatedValue = newValue
                }
            Text(label)
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(0.6)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarInk3)
            Text(sublabel)
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .cavnarCard()
    }
}

/// Interpolates its own numeric value across an implicit animation and
/// re-formats it every intermediate frame — same technique as Home's
/// ValueChartCard, which is what makes the figure visibly count up rather
/// than cross-fade between two static strings.
private struct AnimatableTileNumber: View, Animatable {
    var value: Double
    var format: (Double) -> String

    // nonisolated: SwiftUI drives animatableData from its own animation
    // machinery, which is not guaranteed to run on the main actor. The
    // property only reads/writes stored value types, so leaving it unisolated
    // is both correct and required for the conformance to be valid in the
    // Swift 6 language mode (audit 2.3).
    nonisolated var animatableData: Double {
        get { value }
        set { value = newValue }
    }

    var body: some View {
        Text(format(value))
    }
}
