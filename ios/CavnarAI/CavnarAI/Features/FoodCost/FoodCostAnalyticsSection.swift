import SwiftUI

/// Food Cost Analytics tab — deliberately built around whitespace and
/// typography instead of stacking bordered card after bordered card. Only
/// the hero (annual waste vs. recoverable) is a real container; everything
/// else signals "new section" with a kicker label and generous vertical
/// spacing, and "grouped item" with a hairline divider or a colored
/// left-edge accent bar instead of a box. Matches the same unboxed
/// direction LaborAnalyticsSection's own chart already took (see
/// LaborPerformanceChart's doc comment: "the Analytics tab had become an
/// unbroken column of bordered cards") — Food Cost's own tab had the exact
/// same problem, just one step further along it (every section boxed, not
/// just some).
struct FoodCostAnalyticsSection: View {
    let viewModel: FoodCostAnalyticsViewModel

    var body: some View {
        ScrollView {
            // 44pt between top-level sections — was 28, then 36, still
            // read as crammed once every section lost its own card border
            // (a border used to do double duty as visual separation;
            // without one, the gap between sections has to carry that job
            // alone). Matches the wider rhythm SaaS dashboards (Stripe,
            // Linear) lean on between distinct content blocks specifically
            // because there's no box to signal "new section" otherwise —
            // only whitespace and the kicker label are left to do it.
            VStack(alignment: .leading, spacing: 44) {
                if let analytics = viewModel.analytics {
                    // The AI strip lives INSIDE the hero card itself (see
                    // heroCard's own comment) when there's a hero to embed
                    // into — reads as that card's own footer commentary,
                    // not a second adjacent card. Only the no-hero case
                    // falls back to the self-contained AIConsultantView.
                    if hasHeroData(analytics) {
                        heroCard(analytics, isLoading: viewModel.isLoading)
                    } else {
                        AIConsultantView(
                            title: "Cavnar AI Food Cost Analysis",
                            insight: analytics.insight,
                            isLoading: viewModel.isLoading,
                            showForecastInSheet: false
                        )
                    }
                    statStrip(analytics)
                    if !analytics.wasteItems.isEmpty {
                        FoodCostDonutChart(
                            title: "TOP WASTE OFFENDERS",
                            slices: analytics.wasteItems.map {
                                FoodCostDonutSlice(
                                    id: $0.id, name: $0.item, value: $0.wasteCost,
                                    subtitle: Text(String(format: "%.0f", $0.wastePct)).font(.cavnarNumber(13.5))
                                        + Text("% waste").font(.cavnarBody(13.5))
                                )
                            },
                            centerLabel: "TOTAL"
                        )
                    }
                    if !analytics.overstock.isEmpty {
                        FoodCostDonutChart(
                            title: "OVERSTOCKED — TIED-UP CAPITAL",
                            slices: analytics.overstock.map {
                                FoodCostDonutSlice(
                                    id: $0.id, name: $0.item, value: $0.overstockCost,
                                    subtitle: [$0.currentStock, $0.parLevel].compactMap { $0 }.count == 2
                                        ? Text("\(Int($0.currentStock ?? 0))").font(.cavnarNumber(13.5))
                                            + Text(" / ").font(.cavnarBody(13.5))
                                            + Text("\(Int($0.parLevel ?? 0))").font(.cavnarNumber(13.5))
                                            + Text(" par").font(.cavnarBody(13.5))
                                        : Text("")
                                )
                            },
                            centerLabel: "TIED UP"
                        )
                    }
                    actionSection(analytics)
                    if !analytics.priceWatch.isEmpty {
                        priceWatchDetail(analytics.priceWatch)
                    }
                    FoodCostTrendChart(
                        weeks: viewModel.trend,
                        benchmarkLabel: analytics.benchmarkLabel,
                        wasteRatePct: analytics.wasteRatePct,
                        totalWasteCostWeek: analytics.totalWasteCostWeek
                    )
                } else if viewModel.isLoading {
                    FoodCostAnalyticsSkeleton()
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 26)
        }
    }

    private func hasHeroData(_ a: FoodCostAnalytics) -> Bool {
        (a.annualWasteProjection ?? 0) > 0 || (a.annualRecoverable ?? 0) > 0
    }

    // MARK: - Hero (the one real container on this page)

    // The AI strip is the LAST row inside this same VStack, after a
    // divider — sharing this card's own .background()/.overlay(border)/
    // .clipShape() instead of being a separate card placed underneath it.
    // That's the actual "attached to the hero" ask: one continuous
    // surface, not two adjacent ones with a small gap between them.
    private func heroCard(_ a: FoodCostAnalytics, isLoading: Bool) -> some View {
        let startFromZero = !viewModel.hasPlayedHeroIntro
        return VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 20) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("PROJECTED ANNUAL WASTE")
                        .font(.cavnarBody(13.5, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(Color.cavnarInk.opacity(0.6))
                    HeroAnimatedNumber(numericValue: a.annualWasteProjection ?? 0, tone: Color.cavnarRed, startFromZero: startFromZero)
                    Text("$\((a.monthlyWasteProjection ?? 0).commaFormatted)/mo at current rate")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk.opacity(0.55))
                }
                Spacer(minLength: 12)
                VStack(alignment: .trailing, spacing: 8) {
                    Text("RECOVERABLE / YEAR")
                        .font(.cavnarBody(13.5, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(Color.cavnarInk.opacity(0.6))
                    HeroAnimatedNumber(numericValue: a.annualRecoverable ?? 0, tone: Color.cavnarGreen, startFromZero: startFromZero)
                    Text("$\((a.recoverableMonthly ?? 0).commaFormatted)/mo with better ordering")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk.opacity(0.55))
                        .multilineTextAlignment(.trailing)
                }
            }
            .padding(22)

            Rectangle().fill(Color.cavnarEmber.opacity(0.35)).frame(height: 1)

            AIConsultantEmbeddedStrip(
                title: "Cavnar AI Food Cost Analysis",
                insight: a.insight,
                isLoading: isLoading,
                showForecastInSheet: false
            )
            .padding(.horizontal, 22)
            .padding(.top, 14)
            // The forecast ribbon straddles this card's bottom edge (see
            // .cavnarRibbonHeroAnchor() below) — matches Labor's own
            // identical fix (LaborView's heroCard) so the ribbon's
            // ~34pt-tall pill doesn't touch the AI strip text right above it.
            .padding(.bottom, 22)
        }
        .background(
            LinearGradient(
                colors: [Color.cavnarEmber.opacity(0.5), Color.cavnarEmber.opacity(0.1)],
                startPoint: .topLeading, endPoint: .bottomTrailing
            )
        )
        .overlay(alignment: .top) {
            Rectangle().fill(Color.cavnarEmber.opacity(0.7)).frame(height: 1)
        }
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.cavnarEmber.opacity(0.5), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        // Mimics Labor's forecast ribbon exactly (DesignSystem/
        // HeroForecastRibbon.swift) — reports this card's bottom-center
        // edge up to FoodCostQuickEntryView's root, which renders the
        // pill there via .cavnarHeroForecastRibbon(...).
        .cavnarRibbonHeroAnchor()
        .onAppear { viewModel.markHeroIntroPlayed() }
    }

    /// Count-up-once hero number — same treatment and same reasoning as
    /// LaborAnalyticsSection's SavingsTile, just without the label/sublabel/
    /// card chrome those tiles carry (heroCard already lays that out
    /// around it). The standard cavnarNumberGlow() (a faint 0.5pt shadow +
    /// a soft 6pt colored glow) reads fine on the app's usual near-black
    /// surfaces, but sitting on this card's own warm orange gradient
    /// background it had nothing to separate from — same warm hue on both
    /// sides of the number. A real drop shadow underneath (not just a
    /// glow) gives it something to sit ON TOP OF instead of blending into
    /// the card.
    private struct HeroAnimatedNumber: View {
        let numericValue: Double
        let tone: Color
        let startFromZero: Bool

        @State private var animatedValue: Double = 0

        var body: some View {
            CavnarAnimatableNumber(value: animatedValue, format: { "$\($0.commaFormatted)" })
                .font(.cavnarNumber(27, weight: 700))
                .foregroundStyle(tone)
                .shadow(color: .black.opacity(0.5), radius: 5, x: 0, y: 3)
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
        }
    }

    // MARK: - Stat strip — borderless, hairline dividers instead of tiles

    private func statStrip(_ a: FoodCostAnalytics) -> some View {
        VStack(spacing: 20) {
            statRow([
                ("$\((a.totalWasteCostWeek ?? 0).commaFormatted)", "Waste / wk", Color.cavnarRed),
                ("$\((a.monthlyWasteProjection ?? 0).commaFormatted)", "Proj. / mo", Color.cavnarAmber),
                ("\(a.wasteItems.count)", "Waste items", a.wasteItems.isEmpty ? Color.cavnarGreen : Color.cavnarAmber),
            ])
            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
            statRow([
                ("\(a.criticalLow.count)", "Critical low", a.criticalLow.isEmpty ? Color.cavnarGreen : Color.cavnarRed),
                ("$\((a.totalStockValue ?? 0).commaFormatted)", "Inv. value", Color.cavnarInk),
                ("\(a.totalItems ?? 0)", "Tracked", Color.cavnarInk),
            ])
        }
    }

    private func statRow(_ items: [(String, String, Color)]) -> some View {
        HStack(spacing: 0) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, entry in
                let (value, label, tone) = entry
                VStack(spacing: 6) {
                    Text(value)
                        .font(.cavnarNumber(18, weight: 700))
                        .foregroundStyle(tone)
                    Text(label.uppercased())
                        .font(.cavnarBody(13.5, weight: 700))
                        .tracking(0.6)
                        .foregroundStyle(Color.cavnarInk3)
                }
                .frame(maxWidth: .infinity)
                if index < items.count - 1 {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(width: 1, height: 32)
                }
            }
        }
    }

    // The waste-vs-industry-benchmark readout now lives inside
    // FoodCostTrendChart itself (see its own header + footer caption) —
    // it used to be a fully separate section here, floating between the
    // stat strip and the donut charts with no real connection to either;
    // "waste rate vs. target" is fundamentally the same story the trend
    // chart at the bottom of the page already tells, just a different
    // slice of it (a rate instead of a dollar total), so it reads better
    // folded into that one chart than announced as its own standalone block.

    // MARK: - Action lists — one flowing list, colored accent bars, no boxes

    @ViewBuilder
    private func actionSection(_ a: FoodCostAnalytics) -> some View {
        if !a.criticalLow.isEmpty || !a.reorderSoon.isEmpty || !a.orderReduction.isEmpty {
            VStack(alignment: .leading, spacing: 26) {
                // Matches dashboard.html's "Order List — Recommended
                // Quantities" heading — without it, the ORDER caption on
                // each row's right-side number (below) reads correctly on
                // its own, but the section as a whole had no framing at all.
                Text("ORDER LIST — RECOMMENDED QUANTITIES")
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                if !a.criticalLow.isEmpty {
                    actionGroup(title: "URGENT — ORDER NOW", color: Color.cavnarRed, items: a.criticalLow, showDays: true)
                }
                if !a.reorderSoon.isEmpty {
                    actionGroup(title: "ORDER SOON", color: Color.cavnarAmber, items: a.reorderSoon, showDays: true)
                }
                if !a.orderReduction.isEmpty {
                    actionGroup(title: "REDUCE ORDER", color: Color.cavnarGreen, items: a.orderReduction, showDays: false)
                }
            }
        }
    }

    private func actionGroup(title: String, color: Color, items: [InventoryActionItem], showDays: Bool) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 6) {
                Text(title)
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(color)
                Spacer()
                Text("\(items.count)")
                    .font(.cavnarNumber(14, weight: 700))
                    .foregroundStyle(color)
            }
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    actionRow(item, color: color, showDays: showDays)
                    if index < items.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
    }

    private func actionRow(_ item: InventoryActionItem, color: Color, showDays: Bool) -> some View {
        HStack(spacing: 14) {
            Rectangle().fill(color).frame(width: 2.5)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.item)
                    .font(.cavnarBody(14.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text(subtitle(for: item, showDays: showDays))
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 4) {
                Text(item.orderCaption)
                    .font(.cavnarBody(13.5, weight: 700))
                    .tracking(0.8)
                    .foregroundStyle(Color.cavnarInk3)
                Text(item.suggestedOrderLabel)
                    .font(.cavnarNumber(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                if let savings = item.savingsVsLast, savings != 0 {
                    Text(savings > 0 ? "↓ $\(String(format: "%.2f", savings))" : "↑ $\(String(format: "%.2f", -savings))")
                        .font(.cavnarNumber(14, weight: 700))
                        .foregroundStyle(savings > 0 ? Color.cavnarGreen : Color.cavnarRed)
                }
            }
        }
        .padding(.vertical, 13)
    }

    // "last order" (not bare "last") — the number itself is always a past
    // ORDER quantity, never a stock level, and that wasn't clear before.
    private func subtitle(for item: InventoryActionItem, showDays: Bool) -> String {
        let unitSuffix = item.unit.map { " \($0)" } ?? ""
        let lastQty = item.lastOrderQty.map { $0.truncatingRemainder(dividingBy: 1) == 0 ? "\(Int($0))" : String(format: "%.1f", $0) } ?? "—"
        if showDays, let days = item.daysRemaining {
            let daysStr = days.truncatingRemainder(dividingBy: 1) == 0 ? "\(Int(days))" : String(format: "%.1f", days)
            return "\(daysStr)d left · last order (\(lastQty)\(unitSuffix))"
        }
        return "last order: (\(lastQty)\(unitSuffix))"
    }

    // MARK: - Price Watch (bare kicker heading, matching "TOP WASTE
    // OFFENDERS" / "ORDER LIST — RECOMMENDED QUANTITIES" elsewhere on this
    // page — no background/border treatment, that read as a redundant
    // second banner sitting right above these same rows).

    private func priceWatchDetail(_ items: [PriceWatchItem]) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("PRICE WATCH")
                .font(.cavnarBody(14, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    priceWatchRow(item)
                    if index < items.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
    }

    private func priceWatchRow(_ item: PriceWatchItem) -> some View {
        let accent = item.isTrend ? Color.cavnarRed : Color.cavnarAmber
        return HStack(alignment: .top, spacing: 14) {
            Rectangle().fill(accent).frame(width: 2.5)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.item)
                    .font(.cavnarBody(14.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text("$\(String(format: "%.2f", item.oldPrice)) → $\(String(format: "%.2f", item.newPrice))")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                Text(item.actionHint)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineSpacing(2)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 3) {
                Text("+\(String(format: "%.0f", item.changePct))%")
                    .font(.cavnarNumber(14.5, weight: 700))
                    .foregroundStyle(accent)
                Text(item.timeframeLabel)
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(.vertical, 14)
    }
}

// The forecast pill now mimics Labor's exactly via the shared
// DesignSystem/HeroForecastRibbon.swift component — see
// FoodCostQuickEntryView's .cavnarHeroForecastRibbon(...) call and this
// file's heroCard .cavnarRibbonHeroAnchor().
