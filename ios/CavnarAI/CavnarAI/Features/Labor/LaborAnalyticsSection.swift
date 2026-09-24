import SwiftUI

/// Labor Analytics tab — the gap to target, industry benchmark, and a real
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
                moneyTiles(stats)
                benchmarkBar(stats)
                LaborRibbonChart(points: ribbonPoints, target: stats.target,
                                 subtitle: viewModel.daily.isEmpty ? "8-week trend" : "last \(viewModel.daily.count) days")
                WeekRadarChart(dowSummary: stats.dowSummary, target: stats.target,
                               subtitle: [stats.dateRange.start, stats.dateRange.end].compactMap { $0 }.map(Self.shortDate).joined(separator: " – "),
                               positiveAllowed: Self.positiveAllowed(stats))
            } else if viewModel.isLoading {
                CavnarWorkingLine().padding(.vertical, 20)
            }
        }
    }

    /// "2026-06-01" -> "6/1/26" — the app's date shorthand everywhere else.
    private static func shortDate(_ iso: String) -> String {
        let parts = iso.prefix(10).split(separator: "-")
        guard parts.count == 3, let m = Int(parts[1]), let d = Int(parts[2]) else { return iso }
        return "\(m)/\(d)/\(parts[0].suffix(2))"
    }

    /// Daily labor % when the daily history exists (the Ribbon's real
    /// feed); the 8-week trend otherwise, so the chart never sits empty.
    private var ribbonPoints: [LaborRibbonChart.Point] {
        if !viewModel.daily.isEmpty {
            return viewModel.daily.map { day in
                let label = day.dayOfWeek.map { String($0.prefix(3)) } ?? String(day.date.suffix(5))
                return LaborRibbonChart.Point(id: day.date, label: label, pct: day.laborPct)
            }
        }
        return viewModel.trend.map { LaborRibbonChart.Point(id: $0.label, label: $0.label, pct: $0.pct) }
    }

    @ViewBuilder
    private func moneyTiles(_ stats: LaborStats) -> some View {
        let b = stats.savingsBreakdown
        // Captured once per render, before the .onAppear below flips the
        // flag — every tile in this pass sees the same snapshot, so all
        // four count up together on first load instead of racing each
        // other for which gets to be "first" and flip the shared flag.
        let startFromZero = !viewModel.hasPlayedTilesIntro
        // LazyVGrid doesn't equalise heights across a row, so a tile whose
        // label wraps sat taller than its neighbour. Each tile now fills
        // its cell (see LaborStatTile's maxHeight) inside a grid whose rows
        // are sized by the taller tile.
        // The gap above target is an opportunity, not savings: its label,
        // its period, the warn tone, and no dollars at all on sample data
        // (OwnerCopy.laborMoneyTiles; never-say C, NS1 #2, #15, NS3 C3).
        let money = OwnerCopy.laborMoneyTiles(isLive: stats.isLive, monthly: b.laborMonthly, annual: b.laborAnnual,
                                              vsIndustryMonthly: b.laborVsIndustryMonthly,
                                              vsIndustryAnnual: b.laborVsIndustryAnnual,
                                              industryText: b.industryPctText, periodDays: stats.periodDays)
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
            ForEach(Array(money.enumerated()), id: \.offset) { _, tile in
                LaborStatTile(numericValue: tile.value, format: formattedDollarsK, label: tile.label, sublabel: tile.sublabel,
                            tone: Self.color(tile.tone), startFromZero: startFromZero)
            }
            if stats.isLive && b.laborOvertime > 0 {
                LaborStatTile(numericValue: b.laborOvertime, format: formattedDollarsK, label: "Overtime premium",
                            sublabel: "0.5× rate on hours over 40" + (stats.periodDays.map { " \u{00B7} \($0) days" } ?? ""),
                            tone: Color.cavnarRed, startFromZero: startFromZero)
            }
            LaborStatTile(
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

    /// Semantic tone → colour. Neutral is ink, never green: a gap or a
    /// benchmark difference is not a win.
    static func color(_ tone: OwnerCopy.Tone) -> Color {
        switch tone {
        case .good: return .cavnarGreen
        case .warn: return .cavnarAmber
        case .bad: return .cavnarRed
        case .neutral: return .cavnarInk
        }
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
        let bucket = benchmarkBucket(stats, pct: pct, target: target)
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
                    .font(.cavnarBody(14.5, weight: 700))
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
            // The server's benchmark and its source when sent (I10:
            // thresholds.LABOR_INDUSTRY_PCT, one figure for web and iOS).
            let industryMid = stats.savingsBreakdown.laborIndustryPct ?? (Self.industryLow + Self.industryHigh) / 2
            let diff = pct - industryMid
            let isBelow = diff <= 0
            // Green only when the read may carry a positive word at all.
            let positive = Self.positiveAllowed(stats)
            HomeMixedText.make(Self.industryLine(diff: diff, industryText: stats.savingsBreakdown.industryPctText),
                               size: 14, color: isBelow ? (positive ? .cavnarGreen : .cavnarInk2) : .cavnarRed, numberWeight: 700)
                .fixedSize(horizontal: false, vertical: true)
            if let basis = stats.savingsBreakdown.laborIndustryBasis, !basis.isEmpty {
                HomeMixedText.make("Benchmark: \(basis).", size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .cavnarCard()
    }

    /// "3.2 points below the 34.5% industry benchmark" — a difference of two
    /// percentages is points, and the benchmark is a published figure, not
    /// "other similar restaurants".
    static func industryLine(diff: Double, industryText: String) -> String {
        let pts = String(format: "%.1f", abs(diff))
        return "Your labor is \(pts) point\(pts == "1.0" ? "" : "s") \(diff <= 0 ? "below" : "above") the \(industryText) industry benchmark"
    }

    /// The positive-status contract for labor (OwnerCopy): live, complete,
    /// clocked hours, a full week, and sales under it.
    static func positiveAllowed(_ stats: LaborStats) -> Bool {
        OwnerCopy.laborPositiveAllowed(isLive: stats.isLive, dataComplete: stats.dataComplete,
                                       salesDataMissing: stats.salesDataMissing,
                                       hoursAreEstimated: stats.hoursAreEstimated,
                                       periodTooShort: stats.periodTooShortToProject, pct: stats.overallLaborPct)
    }

    /// "On Target" / "Well Under Target" only under the contract — labor at
    /// 0% with no sales read "Excellent" (NS1 #4, H13). Neutral is ink3.
    private func benchmarkBucket(_ stats: LaborStats, pct: Double, target: Double) -> (label: String, color: Color) {
        let s = OwnerCopy.laborBucket(pct: pct, target: target, isLive: stats.isLive,
                                      positiveAllowed: Self.positiveAllowed(stats))
        return (s.label, s.tone == .neutral ? Color.cavnarInk3 : Self.color(s.tone))
    }
}

private struct LaborStatTile: View {
    let numericValue: Double
    let format: (Double) -> String
    let label: String
    let sublabel: String
    var tone: Color = Color.cavnarInk
    let startFromZero: Bool

    // Same count-up-once treatment as Home's ValueChartCard hero number —
    // a flat instant figure reads as "just a stat," counting up reads as
    // "watch how much this is."
    @State private var animatedValue: Double = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            AnimatableTileNumber(value: animatedValue, format: format)
                .font(.cavnarNumber(26, weight: 700))
                .foregroundStyle(tone)
                .cavnarNumberGlow(tone)
                .cavnarSensitive()
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
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(16)
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
