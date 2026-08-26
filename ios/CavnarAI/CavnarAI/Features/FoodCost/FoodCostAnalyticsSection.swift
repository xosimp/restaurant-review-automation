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
            VStack(alignment: .leading, spacing: 28) {
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
                            isLoading: viewModel.isLoading
                        )
                    }
                    statStrip(analytics)
                    if let label = analytics.benchmarkLabel, let pct = analytics.wasteRatePct, label != "—" {
                        benchmarkBar(label: label, pct: pct)
                    }
                    // Nothing renders at all when prices are flat — the
                    // page stays exactly as lean as it is without this
                    // feature. When something's moving, this teaser sits
                    // high on the page (so it's seen without scrolling past
                    // the waste/overstock donuts) and points at the real
                    // detail list further down, after the order list.
                    if !analytics.priceWatch.isEmpty {
                        priceWatchBanner(analytics.priceWatch)
                    }
                    if !analytics.wasteItems.isEmpty {
                        FoodCostDonutChart(
                            title: "TOP WASTE OFFENDERS",
                            slices: analytics.wasteItems.map {
                                FoodCostDonutSlice(
                                    id: $0.id, name: $0.item, value: $0.wasteCost,
                                    subtitle: Text(String(format: "%.0f", $0.wastePct)).font(.cavnarNumber(9))
                                        + Text("% waste").font(.cavnarBody(9))
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
                                        ? Text("\(Int($0.currentStock ?? 0))").font(.cavnarNumber(9))
                                            + Text(" / ").font(.cavnarBody(9))
                                            + Text("\(Int($0.parLevel ?? 0))").font(.cavnarNumber(9))
                                            + Text(" par").font(.cavnarBody(9))
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
                    FoodCostTrendChart(weeks: viewModel.trend)
                } else if viewModel.isLoading {
                    FoodCostAnalyticsSkeleton()
                }
            }
            .padding(20)
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
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 18) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("PROJECTED ANNUAL WASTE")
                        .font(.cavnarBody(9, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(Color.cavnarInk.opacity(0.6))
                    Text("$\((a.annualWasteProjection ?? 0).commaFormatted)")
                        .font(.cavnarNumber(26, weight: 700))
                        .foregroundStyle(Color.cavnarRed)
                        .cavnarNumberGlow(Color.cavnarRed)
                    Text("$\((a.monthlyWasteProjection ?? 0).commaFormatted)/mo at current rate")
                        .font(.cavnarBody(10))
                        .foregroundStyle(Color.cavnarInk.opacity(0.55))
                }
                Spacer(minLength: 12)
                VStack(alignment: .trailing, spacing: 6) {
                    Text("RECOVERABLE / YEAR")
                        .font(.cavnarBody(9, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(Color.cavnarInk.opacity(0.6))
                    Text("$\((a.annualRecoverable ?? 0).commaFormatted)")
                        .font(.cavnarNumber(26, weight: 700))
                        .foregroundStyle(Color.cavnarGreen)
                        .cavnarNumberGlow(Color.cavnarGreen)
                    Text("$\((a.recoverableMonthly ?? 0).commaFormatted)/mo with better ordering")
                        .font(.cavnarBody(10))
                        .foregroundStyle(Color.cavnarInk.opacity(0.55))
                        .multilineTextAlignment(.trailing)
                }
            }
            .padding(18)

            Rectangle().fill(Color.cavnarEmber.opacity(0.35)).frame(height: 1)

            AIConsultantEmbeddedStrip(
                title: "Cavnar AI Food Cost Analysis",
                insight: a.insight,
                isLoading: isLoading
            )
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
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
    }

    // MARK: - Stat strip — borderless, hairline dividers instead of tiles

    private func statStrip(_ a: FoodCostAnalytics) -> some View {
        VStack(spacing: 14) {
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
                VStack(spacing: 4) {
                    Text(value)
                        .font(.cavnarNumber(18, weight: 700))
                        .foregroundStyle(tone)
                    Text(label.uppercased())
                        .font(.cavnarBody(8, weight: 700))
                        .tracking(0.6)
                        .foregroundStyle(Color.cavnarInk3)
                }
                .frame(maxWidth: .infinity)
                if index < items.count - 1 {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(width: 1, height: 30)
                }
            }
        }
    }

    // MARK: - Benchmark — a bar, not a badge; no card wrapper

    private static let benchmarkScaleMax = 15.0
    private static let industryLow = 4.0
    private static let industryHigh = 5.0
    // A fixed light yellow, not a translucent white overlay — .white.opacity
    // blended with whatever fill tone sits underneath it (green/amber/red
    // depending on this restaurant's current bucket), so the band read as a
    // DIFFERENT color depending on which bucket a restaurant happened to be
    // in, and never matched the legend swatch sitting on plain black beside
    // it. This is the one color used for both, always, regardless of tone.
    private static let industryBandColor = Color(red: 0.95, green: 0.85, blue: 0.45)

    private func benchmarkBar(label: String, pct: Double) -> some View {
        let tone = benchmarkColor(label)
        let fill = min(pct / Self.benchmarkScaleMax, 1)
        let industryStart = Self.industryLow / Self.benchmarkScaleMax
        let industryWidth = (Self.industryHigh - Self.industryLow) / Self.benchmarkScaleMax

        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("WASTE VS. INDUSTRY BENCHMARK")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                Spacer()
                Text(String(format: "%.1f%%", pct))
                    .font(.cavnarNumber(12, weight: 700))
                    .foregroundStyle(tone)
                Text(label)
                    .font(.cavnarBody(9, weight: 700))
                    .foregroundStyle(tone)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                    Capsule().fill(tone)
                        .frame(width: geo.size.width * fill)
                    // Drawn LAST (was second, under the fill capsule) — a
                    // waste rate above 5% is exactly the case worth
                    // flagging, and the fill for anything above 5% already
                    // extends past this band's position, painting over it
                    // when it's drawn first. Solid enough to stay legible
                    // regardless of what's under it.
                    Rectangle().fill(Self.industryBandColor.opacity(0.85))
                        .frame(width: max(geo.size.width * industryWidth, 2))
                        .offset(x: geo.size.width * industryStart)
                }
            }
            .frame(height: 8)
            HStack(spacing: 4) {
                Rectangle().fill(Self.industryBandColor).frame(width: 8, height: 8)
                Text("Industry target: 4–5% of purchases")
            }
            .font(.cavnarBody(9.5))
            .foregroundStyle(Color.cavnarInk3)
        }
    }

    private func benchmarkColor(_ label: String) -> Color {
        switch label {
        case "Excellent", "On Track": return .cavnarGreen
        case "Above Average", "Concerning": return .cavnarAmber
        case "Needs Attention": return .cavnarRed
        default: return .cavnarInk3
        }
    }

    // MARK: - Action lists — one flowing list, colored accent bars, no boxes

    @ViewBuilder
    private func actionSection(_ a: FoodCostAnalytics) -> some View {
        if !a.criticalLow.isEmpty || !a.reorderSoon.isEmpty || !a.orderReduction.isEmpty {
            VStack(alignment: .leading, spacing: 22) {
                // Matches dashboard.html's "Order List — Recommended
                // Quantities" heading — without it, the ORDER caption on
                // each row's right-side number (below) reads correctly on
                // its own, but the section as a whole had no framing at all.
                Text("ORDER LIST — RECOMMENDED QUANTITIES")
                    .font(.cavnarBody(10, weight: 700))
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
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text(title)
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(color)
                Spacer()
                Text("\(items.count)")
                    .font(.cavnarNumber(10, weight: 700))
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
        HStack(spacing: 12) {
            Rectangle().fill(color).frame(width: 2.5)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.item)
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text(subtitle(for: item, showDays: showDays))
                    .font(.cavnarBody(10))
                    .foregroundStyle(Color.cavnarInk3)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 3) {
                Text(item.orderCaption)
                    .font(.cavnarBody(7.5, weight: 700))
                    .tracking(0.8)
                    .foregroundStyle(Color.cavnarInk3)
                Text(item.suggestedOrderLabel)
                    .font(.cavnarNumber(12, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                if let savings = item.savingsVsLast, savings != 0 {
                    Text(savings > 0 ? "↓ $\(String(format: "%.2f", savings))" : "↑ $\(String(format: "%.2f", -savings))")
                        .font(.cavnarNumber(10, weight: 700))
                        .foregroundStyle(savings > 0 ? Color.cavnarGreen : Color.cavnarRed)
                }
            }
        }
        .padding(.vertical, 9)
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

    // MARK: - Price Watch (teaser near the top, detail after the order list)

    private func priceWatchBanner(_ items: [PriceWatchItem]) -> some View {
        HStack(spacing: 9) {
            Circle()
                .fill(Color.cavnarAmber)
                .frame(width: 7, height: 7)
                .shadow(color: Color.cavnarAmber.opacity(0.7), radius: 4)
            Text("Price Watch — ")
                .foregroundStyle(Color.cavnarInk2)
                + Text("\(items.count)")
                    .font(.cavnarNumber(11.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                + Text(items.count == 1 ? " ingredient trending" : " ingredients trending")
                    .foregroundStyle(Color.cavnarInk2)
            Spacer()
            Image(systemName: "chevron.right")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Color.cavnarInk3)
        }
        .font(.cavnarBody(11.5))
        .padding(.horizontal, 13)
        .padding(.vertical, 10)
        .background(Color.cavnarAmber.opacity(0.09))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarAmber.opacity(0.28), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private func priceWatchDetail(_ items: [PriceWatchItem]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("PRICE WATCH")
                .font(.cavnarBody(10, weight: 700))
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
        return HStack(alignment: .top, spacing: 12) {
            Rectangle().fill(accent).frame(width: 2.5)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.item)
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text("$\(String(format: "%.2f", item.oldPrice)) → $\(String(format: "%.2f", item.newPrice))")
                    .font(.cavnarBody(10))
                    .foregroundStyle(Color.cavnarInk3)
                Text(item.actionHint)
                    .font(.cavnarBody(10))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineSpacing(2)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 3) {
                Text("+\(String(format: "%.0f", item.changePct))%")
                    .font(.cavnarNumber(13, weight: 700))
                    .foregroundStyle(accent)
                Text(item.timeframeLabel)
                    .font(.cavnarBody(9))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(.vertical, 10)
    }
}
