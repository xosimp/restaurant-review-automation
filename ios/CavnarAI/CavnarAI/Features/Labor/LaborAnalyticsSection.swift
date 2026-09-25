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
                // How you compare — the Benchmark Engine's card (#23) —
                // after the restaurant's own read and charts (density #34).
                HowYouCompareCard(module: "labor")
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
                                              vsIndustryMonthly: b.laborVsIndustryMonthly ?? 0,
                                              vsIndustryAnnual: b.laborVsIndustryAnnual ?? 0,
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

    @ViewBuilder
    private func benchmarkBar(_ stats: LaborStats) -> some View {
        let pct = stats.overallLaborPct
        let target = max(stats.target, 1)
        let bucket = benchmarkBucket(stats, pct: pct, target: target)
        let barFill = min(pct / 50 * 100, 100)
        let targetPos = min(target / 50 * 100, 97)
        // The industry figure is the server's, by restaurant type
        // (benchmark_registry, NS4 H3) — a published median, drawn as one
        // mark, and absent (no mark, no legend, no line) when the registry
        // has no entry for this type. The client no longer keeps a band.
        let industry = stats.savingsBreakdown.laborIndustryPct
        let industryPos = industry.map { min($0 / 50 * 100, 99) }

        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Labor % vs \(Self.targetName(stats))")
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
                    Capsule().fill(bucket.color)
                        .frame(width: geo.size.width * barFill / 100)
                    Rectangle().fill(Color.cavnarGreen)
                        .frame(width: 2)
                        .offset(x: geo.size.width * targetPos / 100)
                    if let industryPos {
                        Rectangle().fill(Color.cavnarInk.opacity(0.55))
                            .frame(width: 2)
                            .offset(x: geo.size.width * industryPos / 100)
                    }
                }
            }
            .frame(height: 10)
            HStack(spacing: 14) {
                HStack(spacing: 4) {
                    Rectangle().fill(Color.cavnarGreen).frame(width: 8, height: 2)
                    Text(Self.targetLegend(stats, target: target))
                }
                if let ind = stats.savingsBreakdown.industryPctText {
                    HStack(spacing: 4) {
                        Rectangle().fill(Color.cavnarInk.opacity(0.55)).frame(width: 8, height: 2)
                        Text("Published figure (\(ind))")
                    }
                }
            }
            .font(.cavnarBody(14))
            .foregroundStyle(Color.cavnarInk3)

            // The published mark is CONTEXT, never a standing (Benchmarking
            // #2, R1-05): it includes benefits and this figure is wages from
            // shifts, so no points-difference sentence against it —
            // the web never drew one either. Its source and what it measures,
            // only when the server sent one (NS4 H3: no entry, no line).
            if industry != nil, let ind = stats.savingsBreakdown.industryPctText {
                HomeMixedText.make("Published figure \(ind)" + (stats.savingsBreakdown.laborIndustryBasis.map { ": \($0)" } ?? "")
                                   + ". \(Self.industryDefinitionNote)", size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .cavnarCard()
    }

    /// Why the published mark is context and not a comparison: it is not the
    /// same measure as this restaurant's labor %.
    static let industryDefinitionNote = "It includes benefits; yours is wages from your shifts, so it is context, not a like-for-like comparison."

    /// The target as the server names it: "your target" only for one the
    /// owner set, else "Cavnar's starting target" (Benchmarking #10).
    static func targetName(_ stats: LaborStats) -> String {
        stats.savingsBreakdown.laborTargetLabel ?? "your target"
    }

    /// "Cavnar's starting target (30%)" — the legend beside the green mark.
    static func targetLegend(_ stats: LaborStats, target: Double) -> String {
        let name = targetName(stats)
        return name.prefix(1).uppercased() + name.dropFirst() + " (\(Int(target))%)"
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
