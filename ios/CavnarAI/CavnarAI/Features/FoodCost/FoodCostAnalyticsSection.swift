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
    @State private var showingSupplierOrder = false
    @State private var showingMenuMargins = false
    @State private var showingInvoiceScan = false
    @State private var showingRecipes = false
    @State private var showingCountSheet = false
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
                    // Sits above everything else in the module: the numbers
                    // in the cards below are the example pantry's, not this
                    // restaurant's, and they read identically otherwise.
                    if analytics.showsExampleData {
                        CavnarCaveat.exampleData
                    }
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
                            showForecastInSheet: false,
                            recSurface: "food"
                        )
                    }
                    // The module's namesake number, the coverage that says how
                    // far the rest of this screen can be trusted, and the
                    // counted-vs-inferred waste split. All three were computed
                    // server-side and exposed on their own endpoints; this app
                    // called none of them, so the phone showed a confident
                    // waste analysis with no way to see the percentage it was
                    // about or how soft the usage figures underneath were.
                    positionStrip(analytics)
                    statStrip(analytics)
                    // The CFO read: what is DRIVING the cost, ranked by the
                    // dollars each driver carries, the stored root cause with
                    // its alternative and its confidence, and the month-end
                    // prime-cost projection. Everything above is a position;
                    // this is the step after it.
                    if viewModel.hasCFORead {
                        cfoCard(viewModel.cfo)
                    }
                    // Dishes an ingredient rise has eaten into, each with the
                    // price that restores its food cost % and one tap to set
                    // it. Nothing renders when there is nothing to revisit.
                    if !viewModel.repriceSuggestions.isEmpty {
                        repriceSection(viewModel.repriceSuggestions,
                                       assumption: viewModel.reprice?.assumption)
                    }
                    if (analytics.recoverableMonthly ?? 0) > 0 {
                        RecoverableGaugeChart(
                            monthly: analytics.recoverableMonthly ?? 0,
                            annual: analytics.annualRecoverable ?? (analytics.recoverableMonthly ?? 0) * 12,
                            // Recurring waste only. Overstock is capital
                            // sitting above par, not money leaving every
                            // month; adding it made the arc a ratio of two
                            // different kinds of quantity.
                            ceiling: max(analytics.recoverableMonthly ?? 0,
                                         analytics.monthlyWasteProjection ?? 0)
                        )
                    }
                    if !analytics.wasteItems.isEmpty {
                        WasteLedgerChart(
                            kicker: "Top waste offenders", title: "Waste Ledger",
                            // total_waste_cost_week covers every item;
                            // waste_items is only those above their category
                            // tolerance, capped at six. The two branches of
                            // one label used to compute different numbers, so
                            // the fallback is the server's own flagged total.
                            headline: "This week · $\(Int((analytics.wasteItemsTotal ?? analytics.wasteItems.reduce(0) { $0 + $1.wasteCost }).rounded()).formatted()) flagged",
                            rows: analytics.wasteItems.map {
                                WasteLedgerChart.Row(id: $0.id, name: $0.item, value: $0.wasteCost, detail: String(format: "%.0f%% waste", $0.wastePct))
                            }
                        )
                    }
                    if !analytics.overstock.isEmpty {
                        WasteLedgerChart(
                            kicker: "Overstocked", title: "Tied-Up Capital",
                            // The server's total over every overstocked
                            // item — the list here is truncated to five.
                            headline: "$\(Int((analytics.overstockTotal ?? analytics.overstock.reduce(0) { $0 + $1.overstockCost }).rounded()).formatted()) sitting on shelves",
                            rows: analytics.overstock.map {
                                WasteLedgerChart.Row(
                                    id: $0.id, name: $0.item, value: $0.overstockCost,
                                    detail: [$0.currentStock, $0.parLevel].compactMap { $0 }.count == 2 ? "\(Int($0.currentStock ?? 0)) / \(Int($0.parLevel ?? 0)) par" : nil
                                )
                            },
                            tint: Color.cavnarAmber
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
                        totalWasteCostWeek: analytics.totalWasteCostWeek,
                        target: viewModel.trendTarget,
                        asOf: analytics.lastUpdated
                    )
                } else if viewModel.isLoading || !viewModel.hasRequestedFirstLoad {
                    // The first load starts as the tab appears — the
                    // skeleton covers the frame before it does.
                    FoodCostAnalyticsSkeleton()
                } else if let message = viewModel.errorMessage {
                    // A failed load used to render an empty ScrollView: no
                    // message, no retry, and the only way out was leaving the
                    // module entirely.
                    loadFailed(message)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 26)
        }
        .sheet(isPresented: $showingSupplierOrder) {
            SupplierOrderSheet()
        }
        .sheet(isPresented: $showingMenuMargins) {
            MenuMarginsSheet()
        }
        .sheet(isPresented: $showingInvoiceScan) {
            InvoiceScanSheet()
        }
        .sheet(isPresented: $showingRecipes) {
            RecipeDraftsSheet()
        }
        .sheet(isPresented: $showingCountSheet) {
            CountSheetView()
        }
    }

    private func hasHeroData(_ a: FoodCostAnalytics) -> Bool {
        (a.annualWasteProjection ?? 0) > 0 || (a.annualRecoverable ?? 0) > 0
    }

    // MARK: - Load failure

    private func loadFailed(_ message: String) -> some View {
        VStack(spacing: 12) {
            Text("Food cost didn't load")
                .font(.cavnarBody(17, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text(message)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk.opacity(0.6))
                .multilineTextAlignment(.center)
            Button {
                Task { await viewModel.load() }
            } label: {
                Text("Try again")
                    .font(.cavnarBody(15, weight: 600))
                    .padding(.horizontal, 20).padding(.vertical, 10)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.cavnarEmber)
            .overlay(Capsule().stroke(Color.cavnarEmber.opacity(0.5), lineWidth: 1))
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 48)
    }

    // MARK: - Hero (the one real container on this page)

    // The AI strip is the LAST row inside this same VStack, after a
    // divider — sharing this card's own .background()/.overlay(border)/
    // .clipShape() instead of being a separate card placed underneath it.
    // That's the actual "attached to the hero" ask: one continuous
    // surface, not two adjacent ones with a small gap between them.
    // MARK: - Position: the percentage, and how far to trust it

    /// Food cost %, recipe coverage and the counted-vs-inferred waste split.
    ///
    /// Each renders only when its own figure exists. When food cost % cannot
    /// be computed the card says which component is missing rather than
    /// showing nothing — an owner can act on "no closing count" in a way they
    /// cannot act on a blank space.
    @ViewBuilder
    private func positionStrip(_ a: FoodCostAnalytics) -> some View {
        if a.cogs != nil || a.recipeCoverage != nil || a.wasteSplit != nil {
            VStack(alignment: .leading, spacing: 12) {
                if let c = a.cogs {
                    if c.ok, let pct = c.pct {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("FOOD COST")
                                .font(.cavnarBody(11, weight: 700)).tracking(1.2)
                                .foregroundStyle(Color.cavnarInk3)
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Text("\(pct, specifier: "%.1f")%")
                                    .font(.cavnarNumber(34, weight: 600))
                                    .foregroundStyle(Self.toneColor(c.tone))
                                if let v = c.variancePts, let t = c.target {
                                    Text("\(v > 0 ? "+" : "")\(v, specifier: "%.1f") pts vs \(t, specifier: "%.0f")% target")
                                        .font(.cavnarBody(13.5))
                                        .foregroundStyle(v > 0 ? Color.cavnarRed : Color.cavnarGreen)
                                } else if let label = c.label {
                                    Text(label).font(.cavnarBody(13.5))
                                        .foregroundStyle(Color.cavnarInk3)
                                }
                            }
                            if let basis = c.basis {
                                Text(basis).font(.cavnarBody(11.5))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("Food cost \(String(format: "%.1f", pct)) percent of sales")
                    } else if let missing = c.missing, !missing.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("FOOD COST — NOT YET MEASURABLE")
                                .font(.cavnarBody(11, weight: 700)).tracking(1.2)
                                .foregroundStyle(Color.cavnarInk3)
                            ForEach(missing, id: \.self) { m in
                                Text("· \(m.component): \(m.why)")
                                    .font(.cavnarBody(12.5))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                let trustLines = Self.trustLines(a, cfoTrust: viewModel.cfo?.brief?.trust)
                if !trustLines.isEmpty {
                    HomeMixedText.make(trustLines.joined(separator: "  ·  "), size: 12, weight: 400, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// How far the numbers above can be trusted. The CFO brief's `trust`
    /// (the same two measures, from food_cost_intelligence) fills in when
    /// the analytics payload lacks one — it was decoded and never shown.
    static func trustLines(_ a: FoodCostAnalytics, cfoTrust: FoodCostCFO.Brief.Trust? = nil) -> [String] {
        var out: [String] = []
        if let pct = a.recipeCoverage?.coveragePct ?? cfoTrust?.recipeCoveragePct {
            out.append("recipes cover \(Int(pct))% of what sold")
        }
        if let pct = a.wasteSplit?.inferredPct ?? cfoTrust?.inferredWastePct {
            out.append("\(Int(pct))% of waste is an unexplained count gap")
        }
        if a.windowFromCounts == false {
            out.append("no count dates on file — window is approximate")
        } else if let age = a.windowAgeDays, age > 8 {
            out.append("newest count is \(age) days old")
        }
        return out
    }

    private static func toneColor(_ tone: String?) -> Color {
        switch tone {
        case "good":  return .cavnarGreen
        case "warn":  return .cavnarAmber
        case "bad":   return .cavnarRed
        default:      return .cavnarInk
        }
    }

    // MARK: - The CFO read

    /// What is driving the cost, what it is worth, and what to do first.
    ///
    /// The drivers arrive already ranked by dollars, then confidence, then
    /// ease, and are rendered in that order — never re-sorted here, because
    /// the ranking is computed server-side precisely so two clients cannot
    /// disagree about which opportunity is the biggest.
    @ViewBuilder
    private func cfoCard(_ cfo: FoodCostCFO?) -> some View {
        if let cfo {
            let dg = cfo.diagnosis
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("Where the money is going")
                        .font(.cavnarBody(11, weight: 700)).tracking(1.1)
                        .textCase(.uppercase)
                        .foregroundStyle(Color.cavnarEmber)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 16).padding(.top, 14)

                if let total = cfo.drivers?.atStake, total > 0 {
                    HomeMixedText.make("$\(total.commaFormatted)/month across \(viewModel.drivers.count) driver\(viewModel.drivers.count == 1 ? "" : "s")"
                         + (dg?.asOf.map { " · read \($0)" } ?? "")
                         + ((dg?.stale ?? false) && dg?.staleNote == nil ? " · older read" : ""),
                                       size: 12, weight: 400, color: .cavnarInk3)
                        .padding(.horizontal, 16).padding(.top, 4)
                }
                // The server's own sentence for a read that hasn't been
                // refreshed — the same caveat the Reviews diagnosis carries.
                if let note = dg?.staleNote, !note.isEmpty {
                    CavnarCaveat(title: "Older read", detail: note)
                        .padding(.horizontal, 16).padding(.top, 10)
                }
                if let figs = dg?.unsupportedFigures, !figs.isEmpty {
                    CavnarCaveat.unverifiedFigures(figs)
                        .padding(.horizontal, 16).padding(.top, 10)
                }

                VStack(alignment: .leading, spacing: 14) {
                    if let headline = dg?.headline {
                        Text(headline)
                            .font(.cavnarBody(16, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let p = cfo.profitability, p.available,
                       let prime = p.primeCostPct, let projected = p.projectedPrimeCost {
                        profitabilityBlock(p, prime: prime, projected: projected)
                    }
                    if let cause = dg?.cause {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                cfoRow("Most likely cause", cause)
                                // The cause is the model's read — said so.
                                ClaimKindTag(kind: cfo.claimKinds?["why"])
                            }
                            // How sure, as a percentage with what it rests on
                            // (K1/K6) — the shared line, not a bare capsule.
                            if let c = dg?.trust {
                                ConfidenceLine(confidence: c, recKey: dg?.recKey, surface: "food", module: "food")
                            }
                        }
                    }
                    if let alt = dg?.alternativeCause { cfoRow("It could also be", alt, quiet: true) }
                    if let confirm = dg?.whatWouldConfirm { cfoRow("What would tell them apart", confirm) }
                    // An answered action drops with its controls; the
                    // evidence stays.
                    if let action = dg?.recommendedAction, dg?.answered != true {
                        VStack(alignment: .leading, spacing: 6) {
                            cfoRow("Do this first", action)
                            if let key = dg?.recKey {
                                RecAnswerRow(key: key, surface: "food")
                            }
                        }
                    }
                    if let outcome = dg?.expectedOutcome { cfoRow("What should change", outcome, quiet: true) }
                    if let oe = dg?.operationalEvidence, !oe.isEmpty {
                        cfoRow("Cross-checked against",
                               oe.map { "\($0.module): \($0.metric) \($0.value)" }
                                 .joined(separator: "  ·  "), quiet: true)
                    }
                    if !viewModel.drivers.isEmpty {
                        driverList(viewModel.drivers, claimKind: cfo.claimKinds?["drivers"])
                    }
                }
                .padding(16)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.cavnarInk3.opacity(0.05),
                        in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                .stroke(Color.cavnarInk3.opacity(0.18), lineWidth: 1))
        }
    }

    private static func profitabilitySentence(_ p: FoodCostCFO.Profitability,
                                              projected: Double) -> String {
        var s = "month to date"
        if let food = p.foodCostPct, let labor = p.laborPct {
            s += String(format: " (food %.1f%% + labor %.1f%%)", food, labor)
        }
        s += ". At this run rate the month lands near $\(projected.commaFormatted)"
        if let sales = p.projectedSales {
            s += " on $\(sales.commaFormatted) of sales"
        }
        if let delta = p.dollarsVsLastMonth {
            let word = delta > 0 ? "worse" : "better"
            s += " — $\(abs(delta).commaFormatted) \(word) than last month"
        }
        return s + "."
    }

    private func profitabilityBlock(_ p: FoodCostCFO.Profitability,
                                    prime: Double, projected: Double) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(prime, specifier: "%.1f")% prime cost")
                .font(.cavnarNumber(21, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            // Built up in statements rather than one concatenated expression:
            // the single-expression version pushed the type checker past its
            // budget and failed the build outright.
            Text(Self.profitabilitySentence(p, projected: projected))
                .font(.cavnarBody(12.5))
                .foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            if let basis = p.basis {
                Text("Projection, not a measurement. \(basis)")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color.cavnarEmber.opacity(0.09), in: RoundedRectangle(cornerRadius: 9))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Forecast. Prime cost \(String(format: "%.1f", prime)) percent month to date.")
    }

    private func driverList(_ drivers: [FoodCostCFO.Driver], claimKind: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("Ranked by dollars, then confidence, then ease")
                    .font(.cavnarBody(10, weight: 700)).tracking(0.9)
                    .textCase(.uppercase)
                    .foregroundStyle(Color.cavnarInk3)
                Spacer(minLength: 0)
                ClaimKindTag(kind: claimKind)
            }
            ForEach(Array(drivers.prefix(5))) { d in
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("$\(d.dollarsMonthly.commaFormatted)")
                            .font(.cavnarNumber(16, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        Text(d.label)
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HomeMixedText.make(d.evidence, size: 12, weight: 400, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    // How sure, on the shared line (K1); the effort and
                    // what happens if it is ignored stay beside it.
                    if let c = d.trust {
                        ConfidenceLine(confidence: c, recKey: d.recKey, surface: "food", module: "food")
                    }
                    HomeMixedText.make("\(d.difficulty) effort · if ignored: \(d.ifIgnored)",
                                       size: 11.5, weight: 400, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
                // .contain, not .combine: the "Why?" button inside must
                // stay reachable on its own.
                .accessibilityElement(children: .contain)
            }
        }
    }

    private func cfoRow(_ label: String, _ body: String, quiet: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(.cavnarBody(10, weight: 700)).tracking(0.9)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarInk3)
            Text(body)
                .font(.cavnarBody(quiet ? 13 : 14.5))
                .foregroundStyle(quiet ? Color.cavnarInk3 : Color.cavnarInk2)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label). \(body)")
    }

    private func heroCard(_ a: FoodCostAnalytics, isLoading: Bool) -> some View {
        let startFromZero = !viewModel.hasPlayedHeroIntro
        return VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 20) {
                VStack(alignment: .leading, spacing: 8) {
                    // Both kickers reserve two lines. "PROJECTED ANNUAL
                    // WASTE" wraps to two at this width while "RECOVERABLE
                    // / YEAR" fits on one, so with a .top-aligned HStack
                    // the right column's number and subtext sat a full line
                    // higher than the left's. Reserving the space on both
                    // makes the two numbers share a baseline at any width.
                    Text("PROJECTED ANNUAL WASTE")
                        .font(.cavnarBody(13.5, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(Color.cavnarInk.opacity(0.6))
                        .lineLimit(2, reservesSpace: true)
                    HeroAnimatedNumber(numericValue: a.annualWasteProjection ?? 0, tone: Color.cavnarRed, startFromZero: startFromZero)
                    // The basis, stated. This is one week's count projected
                    // to a year; as a bare number in red it reads as measured
                    // fact, and one heavy prep week becomes a five-figure
                    // headline an owner may take to a supplier.
                    Text("$\((a.monthlyWasteProjection ?? 0).commaFormatted)/mo — projected from this week's count")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk.opacity(0.55))
                }
                Spacer(minLength: 12)
                VStack(alignment: .trailing, spacing: 8) {
                    Text("RECOVERABLE / YEAR")
                        .font(.cavnarBody(13.5, weight: 700))
                        .tracking(1.4)
                        .foregroundStyle(Color.cavnarInk.opacity(0.6))
                        .lineLimit(2, reservesSpace: true)
                        .multilineTextAlignment(.trailing)
                    HeroAnimatedNumber(numericValue: a.annualRecoverable ?? 0, tone: Color.cavnarGreen, startFromZero: startFromZero)
                    Text("$\((a.recoverableMonthly ?? 0).commaFormatted)/mo — waste above tolerance")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk.opacity(0.55))
                        .multilineTextAlignment(.trailing)
                }
            }
            .padding(22)

            Rectangle().fill(Color.cavnarEmber.opacity(0.35)).frame(height: 1)

            VStack(alignment: .leading, spacing: 10) {
                AIConsultantEmbeddedStrip(
                    title: "Cavnar AI Food Cost Analysis",
                    insight: a.insight,
                    isLoading: isLoading,
                    showForecastInSheet: false,
                    recSurface: "food"
                )
                // The server computes this flag and every other module renders
                // it; Food Cost declared no such key, so figures it could not
                // trace back to the data were shown at full authority.
                if a.hasUnverifiedFigures {
                    CavnarCaveat.unverifiedFigures(a.unverifiedFigureList)
                }
            }
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
        }
    }

    // MARK: - Stat strip — borderless, hairline dividers instead of tiles

    private func statStrip(_ a: FoodCostAnalytics) -> some View {
        VStack(spacing: 20) {
            statRow([
                (Self.money(a.totalWasteCostWeek), "Waste / wk", Color.cavnarRed),
                (Self.money(a.monthlyWasteProjection), "Proj. / mo", Color.cavnarAmber),
                ("\(a.wasteItems.count)", "Waste items", a.wasteItems.isEmpty ? Color.cavnarGreen : Color.cavnarAmber),
            ])
            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
            statRow([
                ("\(a.criticalLow.count)", "Critical low", a.criticalLow.isEmpty ? Color.cavnarGreen : Color.cavnarRed),
                (Self.money(a.totalStockValue), "Inv. value", Color.cavnarInk),
                (a.totalItems.map(String.init) ?? "—", "Tracked", Color.cavnarInk),
            ])
            if let asOf = a.lastUpdated, !asOf.isEmpty {
                // The server has always sent week_start/week_end/last_updated
                // and nothing rendered them, so an owner could not tell
                // whether the annual projection above came from a count taken
                // today or three weeks ago.
                Text("From your count of \(asOf)")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk.opacity(0.45))
            }
        }
    }

    /// A dollar figure, or an em dash. `?? 0` turns "the field was absent"
    /// into "$0" — and $0 of waste and no waste measurement at all are very
    /// different things to show an owner.
    private static func money(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "$\(value.commaFormatted)"
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

                // The order list used to end here — computed quantities and
                // no way to act on them. This is the step that actually
                // sends it to the supplier who fills it.
                if !a.criticalLow.isEmpty || !a.reorderSoon.isEmpty {
                    Button {
                        Haptic.light()
                        showingSupplierOrder = true
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "paperplane.fill").font(.system(size: 13, weight: .semibold))
                            Text("Send order to suppliers")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: false))
                }

                // Recipes have costed the plate all along; this is where
                // that cost meets the menu price and becomes a margin.
                Button {
                    Haptic.light()
                    showingMenuMargins = true
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "chart.pie.fill").font(.system(size: 13, weight: .semibold))
                        Text("Menu margins")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())

                Button {
                    Haptic.light()
                    showingInvoiceScan = true
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "doc.text.viewfinder").font(.system(size: 13, weight: .semibold))
                        Text("Scan an invoice")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        showingRecipes = true
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "list.bullet.clipboard").font(.system(size: 13, weight: .semibold))
                            Text("Recipes")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    Button {
                        Haptic.light()
                        showingCountSheet = true
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "checklist").font(.system(size: 13, weight: .semibold))
                            Text("Count sheet")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
            }
        } else {
            // Menu margins used to live inside the order-list guard above, so
            // a restaurant with a healthy pantry — or no inventory at all —
            // lost the entire pricing feature, with no other way in.
            Button {
                Haptic.light()
                showingMenuMargins = true
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "chart.pie.fill").font(.system(size: 13, weight: .semibold))
                    Text("Menu margins")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            Button {
                Haptic.light()
                showingInvoiceScan = true
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "doc.text.viewfinder").font(.system(size: 13, weight: .semibold))
                    Text("Scan an invoice")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    showingRecipes = true
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "list.bullet.clipboard").font(.system(size: 13, weight: .semibold))
                        Text("Recipes")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                Button {
                    Haptic.light()
                    showingCountSheet = true
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "checklist").font(.system(size: 13, weight: .semibold))
                        Text("Count sheet")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
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

    // MARK: - Prices to revisit
    //
    // Same anatomy as Price Watch below (bare kicker, accent-bar rows,
    // hairline dividers): the dish, why (the ingredient that moved), now →
    // suggested price, and what the rise costs a month. One primary tap sets
    // the price; "Not for us" answers the recommendation so it stays gone.

    private static func price(_ v: Double) -> String { String(format: "$%.2f", v) }

    private func repriceSection(_ items: [RepriceSuggestions.Suggestion], assumption: String?) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("PRICES TO REVISIT")
                .font(.cavnarBody(14, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    repriceRow(item)
                    if index < items.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
            if let assumption, !assumption.isEmpty {
                Text(assumption)
                    .font(.cavnarBody(11.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func repriceRow(_ s: RepriceSuggestions.Suggestion) -> some View {
        let applied = viewModel.repriceApplied[s.dish]
        let dismissed = viewModel.repriceDismissed.contains(s.dish)
        let busy = viewModel.repriceBusy.contains(s.dish)
        return HStack(alignment: .top, spacing: 14) {
            Rectangle().fill(Color.cavnarAmber).frame(width: 2.5)
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline) {
                    Text(s.dish)
                        .font(.cavnarBody(14.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    // Dollars a month when there is a sales mix; the
                    // per-plate figure when there isn't — never a $0.
                    if let monthly = s.monthlyMarginLost {
                        Text("$\(monthly.commaFormatted)/mo")
                            .font(.cavnarNumber(14.5, weight: 700))
                            .foregroundStyle(Color.cavnarRed)
                    } else if let perPlate = s.increasePerPlate {
                        Text("+\(Self.price(perPlate))/plate")
                            .font(.cavnarNumber(14.5, weight: 700))
                            .foregroundStyle(Color.cavnarRed)
                    }
                }
                if let why = s.whyLine {
                    HomeMixedText.make("Why: \(why)", size: 13.5, color: .cavnarInk3)
                }
                if let now = s.sellPrice, let suggested = s.suggestedPrice {
                    (Text(Self.price(now)) + Text("  \u{2192}  ") + Text(Self.price(suggested)).foregroundStyle(Color.cavnarInk))
                        .font(.cavnarNumber(14, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                        .accessibilityLabel("Now \(Self.price(now)), suggested \(Self.price(suggested))")
                }
                if s.monthlyMarginLost != nil, let basis = s.monthlyBasis {
                    Text("Margin lost a month, from \(basis).")
                        .font(.cavnarBody(11.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let applied {
                    HStack(spacing: 6) {
                        Image(systemName: "checkmark")
                            .font(.system(size: 10, weight: .bold))
                            .accessibilityHidden(true)
                        (Text("Set to ") + Text(Self.price(applied)).font(.cavnarNumber(12.5, weight: 600)))
                            .font(.cavnarBody(12.5, weight: 500))
                    }
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.top, 4)
                    if let tracking = viewModel.repriceTracking[s.dish] {
                        RecTrackerLine(text: tracking)
                    }
                } else {
                    HStack(alignment: .center, spacing: 16) {
                        if let suggested = s.suggestedPrice, !dismissed {
                            Button {
                                Task { await viewModel.applyReprice(s) }
                            } label: {
                                if busy {
                                    CavnarShimmerText(text: "Setting\u{2026}")
                                } else {
                                    (Text("Set ") + Text(Self.price(suggested)).font(.cavnarNumber(16, weight: 600)))
                                }
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy))
                            .disabled(busy)
                            .accessibilityHint("Changes \(s.dish)'s menu price")
                        }
                        if let key = s.recKey {
                            RecAnswerRow(key: key, surface: "food", answers: [.notForUs],
                                         onAnswered: { _ in viewModel.repriceDismissed.insert(s.dish) })
                                .disabled(busy)
                        }
                    }
                    .padding(.top, 6)
                    if let error = viewModel.repriceErrors[s.dish] {
                        Text(error)
                            .font(.cavnarBody(12.5))
                            .foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .padding(.vertical, 14)
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
                // Sign from the value. The "+" was hardcoded, which was only
                // ever safe because the server detected increases and never
                // drops — it now surfaces both, and a drop would have
                // rendered as "+-6%".
                Text("\(item.changePct >= 0 ? "+" : "")\(String(format: "%.0f", item.changePct))%")
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
