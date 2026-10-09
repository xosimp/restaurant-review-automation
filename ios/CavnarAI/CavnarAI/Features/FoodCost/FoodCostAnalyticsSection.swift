import SwiftUI

/// Food Cost Analytics tab — "Web explains. iPhone decides." (iOS
/// readability round, 10/8/26). The tab answers in one pass, top to bottom:
///
///   1. the hero — food cost % against target, this week's waste and the
///      recoverable run-rate, and Cavnar AI's one-paragraph why;
///   2. the decision card — what is driving the cost, the one thing to do
///      first, the top driver's dollars and where to act on it, with the
///      rest of the read behind "Show the full read";
///   3. what to order now, sent through the order sheet;
///   4. dishes to reprice, one Set button each;
///   5. pars to raise;
///   6. one "More analysis" disclosure (the ledgers, price watch, the
///      waste trend's read) with the trend and How you compare on the web.
///
/// It used to stack about thirteen sections, three separate "why" reads, a
/// stat strip that repeated the urgent count and a second row of every
/// action-row button under the order list.
struct FoodCostAnalyticsSection: View {
    let viewModel: FoodCostAnalyticsViewModel
    /// Opens a Food Cost action — the order sheet, a cost driver's own
    /// button ("Open the order", "Look at X's price", "Log or count it",
    /// parity audit #76). The action row's sheets are the screen's, opened
    /// through here, never a second copy of them in this tab.
    var open: ((FoodCostAction) -> Void)? = nil

    @State private var showingMore = false
    /// Dishes whose "Why" is open under their reprice row.
    @State private var repriceWhyOpen: Set<String> = []

    var body: some View {
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: CavnarSpace.xxl) {
                if let analytics = viewModel.analytics {
                    CachedDataNotice(text: viewModel.stalenessNotice)
                    // Sits above everything else in the module: the numbers
                    // in the cards below are the example pantry's, not this
                    // restaurant's, and they read identically otherwise.
                    if analytics.showsExampleData {
                        CavnarCaveat.exampleData
                    }
                    VStack(alignment: .leading, spacing: CavnarSpace.s) {
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
                        // How current the sources behind food cost are.
                        DataHealthModuleBadge(module: "food_cost")
                    }
                    // The decision: what is driving the cost and the one
                    // thing to do first. Everything above is a position;
                    // this is the step after it.
                    if viewModel.hasCFORead {
                        decisionCard(viewModel.cfo)
                    }
                    // What to order now — the page's actions.
                    orderSection(analytics)
                    // Dishes an ingredient rise has eaten into, each with the
                    // price that restores its food cost % and one tap to set
                    // it. Nothing renders when there is nothing to revisit.
                    if !viewModel.repriceSuggestions.isEmpty || !viewModel.scorecardReprice.isEmpty {
                        repriceSection(viewModel.repriceSuggestions,
                                       assumption: viewModel.reprice?.assumption)
                    }
                    // Pars the 86s say are too low (memory round) — each
                    // one raised or declined in place.
                    if !viewModel.parSuggestions.isEmpty {
                        parSection(viewModel.parSuggestions)
                            .id("pars")
                    }
                    moreAnalysis(analytics)
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
            .padding(.horizontal, CavnarSpace.gutter)
            .padding(.vertical, CavnarSpace.xl)
        }
        // nav "inventory/pars" — the par section brought into view.
        .onChange(of: viewModel.scrollToPars && !viewModel.parSuggestions.isEmpty, initial: true) { _, go in
            guard go else { return }
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(350))
                withAnimation(.easeOut(duration: 0.4)) { proxy.scrollTo("pars", anchor: .top) }
                viewModel.scrollToPars = false
            }
        }
        }
    }

    // MARK: - Section title

    /// A section's title: a plain sentence-case headline, not a shouted
    /// kicker ("ORDER LIST — RECOMMENDED QUANTITIES" was one).
    private func sectionTitle(_ title: String, count: Int? = nil) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Text(title).cavnarText(.headline)
            if let count {
                Text("\(count)").cavnarText(.figureS, color: .cavnarInk3)
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    // MARK: - More analysis (L2 and L3)

    /// The ledgers, price watch and the waste trend's read, behind one tap;
    /// the trend chart and How you compare open on the web.
    private func moreAnalysis(_ a: FoodCostAnalytics) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.l) {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.22)) { showingMore.toggle() }
            } label: {
                HStack(spacing: CavnarSpace.xs) {
                    Text(showingMore ? "Less analysis" : "More analysis").cavnarText(.label, color: .cavnarEmber2)
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .rotationEffect(.degrees(showingMore ? 180 : 0))
                        .accessibilityHidden(true)
                    Spacer(minLength: 0)
                }
                .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityValue(showingMore ? "Expanded" : "Collapsed")

            if showingMore {
                VStack(alignment: .leading, spacing: CavnarSpace.xxl) {
                    // The two ledgers side by side on an iPad when both have
                    // rows (#99); stacked on the phone.
                    if !a.wasteItems.isEmpty && !a.overstock.isEmpty {
                        CavnarTwoUp {
                            wasteLedger(a)
                        } trailing: {
                            overstockLedger(a)
                        }
                    } else {
                        if !a.wasteItems.isEmpty { wasteLedger(a) }
                        if !a.overstock.isEmpty { overstockLedger(a) }
                    }
                    if !a.priceWatch.isEmpty {
                        priceWatchDetail(a.priceWatch)
                    }
                    wasteTrendRead(a)
                    VStack(alignment: .leading, spacing: 0) {
                        CavnarWebLinkRow(title: "Waste trend", subtitle: "Every week on file, against your target",
                                         path: "inventory/waste", actionLabel: "Open on the web")
                        CavnarWebLinkRow(title: "How you compare", subtitle: "Food cost against restaurants like yours",
                                         path: "inventory", actionLabel: "Open on the web")
                    }
                }
                .transition(.opacity)
            }
        }
    }

    /// What the waste series shows, said — every figure is one the trend
    /// holds (waste_trend.waste_trend_observations) — or why there is no
    /// trend yet; then the annual projection with its basis, never as a
    /// headline (one week's count projected to a year).
    @ViewBuilder
    private func wasteTrendRead(_ a: FoodCostAnalytics) -> some View {
        let projection = Self.projectionLine(a)
        if !viewModel.trendObservations.isEmpty || viewModel.trendEmpty?.title != nil || projection != nil {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                CavnarKicker("Waste trend")
                if !viewModel.trendObservations.isEmpty {
                    ForEach(viewModel.trendObservations) { o in
                        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                            Circle().fill(Self.toneColor(o.tone == "neutral" ? nil : o.tone)).frame(width: 7, height: 7)
                                .accessibilityHidden(true)
                            VStack(alignment: .leading, spacing: 2) {
                                CavnarMixedText(o.text, role: .secondary)
                                if let c = o.confidence {
                                    CavnarMixedText(c, role: .caption)
                                }
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                } else if let e = viewModel.trendEmpty, let title = e.title {
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        Text(title).cavnarText(.label, color: .cavnarInk2)
                        ForEach([e.reason, e.needed, e.when].compactMap { $0 }, id: \.self) { line in
                            CavnarMixedText(line, role: .caption)
                        }
                    }
                }
                if let projection {
                    CavnarMixedText(projection, role: .caption)
                }
            }
        }
    }

    /// The hero draws when it has its position (food cost %, or the reason
    /// it can't be measured) or a waste figure to carry.
    private func hasHeroData(_ a: FoodCostAnalytics) -> Bool {
        a.cogs != nil || a.recipeCoverage != nil || a.wasteSplit != nil
            || a.totalWasteCostWeek != nil
            || (a.annualWasteProjection ?? 0) > 0 || (a.annualRecoverable ?? 0) > 0
    }

    /// "Projected: $X/mo, $Y/yr — from this week's count only." The basis
    /// travels with the figure: one heavy prep week must not become a
    /// five-figure headline an owner takes to a supplier. Nil when nothing
    /// was projected.
    static func projectionLine(_ a: FoodCostAnalytics) -> String? {
        guard let annual = a.annualWasteProjection, annual > 0 else { return nil }
        let monthly = a.monthlyWasteProjection.map { "$\($0.commaFormatted)/mo, " } ?? ""
        return "Projected waste: \(monthly)$\(annual.commaFormatted)/yr \u{2014} from this week\u{2019}s count only."
    }

    // MARK: - Load failure

    private func loadFailed(_ message: String) -> some View {
        VStack(spacing: CavnarSpace.s) {
            Text("Food cost didn't load").cavnarText(.lead)
            Text(message)
                .cavnarText(.secondary)
                .multilineTextAlignment(.center)
            Button {
                Task { await viewModel.load() }
            } label: {
                Text("Try again")
                    .cavnarText(.label, color: .cavnarEmber)
                    .padding(.horizontal, CavnarSpace.l)
                    .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .overlay(Capsule().stroke(Color.cavnarEmber.opacity(0.5), lineWidth: 1))
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 48)
    }

    // MARK: - Position: the percentage, and how far to trust it

    /// Food cost %, recipe coverage and the counted-vs-inferred waste split.
    ///
    /// Each renders only when its own figure exists. When food cost % cannot
    /// be computed the card says which component is missing rather than
    /// showing nothing — an owner can act on "no closing count" in a way they
    /// cannot act on a blank space.
    @ViewBuilder
    private func positionContent(_ a: FoodCostAnalytics) -> some View {
        if a.cogs != nil || a.recipeCoverage != nil || a.wasteSplit != nil {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if let c = a.cogs {
                    if c.ok, let pct = c.pct {
                        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                            // The screen's one FigureXL: food cost % in its
                            // status colour, the words beside it.
                            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                                Text("\(pct, specifier: "%.1f")%")
                                    .cavnarText(.figureXL, color: Self.toneColor(c.tone))
                                    .minimumScaleFactor(0.85)
                                    .lineLimit(1)
                                Text("food cost").cavnarText(.lead, color: .cavnarInk2)
                            }
                            if let v = c.variancePts, let t = c.target {
                                CavnarMixedText(c.varianceLine(v, t), role: .secondary,
                                                color: c.targetIsOwnersOrSeeded
                                                    ? (v > 0 ? Color.cavnarRedText : Color.cavnarGreen)
                                                    : Color.cavnarInk2)
                            } else if let label = c.label {
                                Text(label).cavnarText(.secondary)
                            }
                            if let basis = c.basis {
                                Text(basis)
                                    .cavnarText(.caption)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("Food cost \(String(format: "%.1f", pct)) percent of sales")
                    } else if let missing = c.missing, !missing.isEmpty {
                        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                            Text("Food cost can\u{2019}t be measured yet").cavnarText(.label)
                            ForEach(missing, id: \.self) { m in
                                Text("\(m.component): \(m.why)")
                                    .cavnarText(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                let trustLines = Self.trustLines(a, cfoTrust: viewModel.cfo?.brief?.trust)
                if !trustLines.isEmpty {
                    CavnarMixedText(trustLines.joined(separator: "  \u{00B7}  "), role: .caption)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// How far the numbers above can be trusted. The CFO brief's `trust`
    /// (the same two measures, from food_cost_intelligence) fills in when
    /// the analytics payload lacks one — it was decoded and never shown.
    /// The count date is the day stock was last counted (`counted_to`), not
    /// when the analysis ran: "From your count of" used to print the run
    /// time (`last_updated`).
    static func trustLines(_ a: FoodCostAnalytics, cfoTrust: FoodCostCFO.Brief.Trust? = nil) -> [String] {
        var out: [String] = []
        if let pct = a.recipeCoverage?.coveragePct ?? cfoTrust?.recipeCoveragePct {
            out.append("recipes cover \(Int(pct))% of what sold")
        }
        if let pct = a.wasteSplit?.inferredPct ?? cfoTrust?.inferredWastePct {
            out.append("\(Int(pct))% of waste is an unexplained count gap")
        }
        if a.windowFromCounts == false {
            out.append("no count dates on file \u{2014} window is approximate")
        } else if let counted = a.countedTo, !counted.isEmpty {
            let day = CavnarDate.mdy(counted)
            if let age = a.windowAgeDays, age > 8 {
                out.append("last count \(day), \(age) days ago")
            } else {
                out.append("last count \(day)")
            }
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

    // MARK: - The decision card

    /// What is driving the cost, what to do first, and what the top driver
    /// is worth — on the answer card's anatomy. The drivers arrive already
    /// ranked by dollars, then confidence, then ease, and are rendered in
    /// that order — never re-sorted here, because the ranking is computed
    /// server-side precisely so two clients cannot disagree about which
    /// opportunity is the biggest. Driver 1 is on the card; drivers 2–5,
    /// what would confirm the cause, the cross-checks and the prime-cost
    /// projection are behind "Show the full read".
    @ViewBuilder
    private func decisionCard(_ cfo: FoodCostCFO?) -> some View {
        if let cfo {
            let dg = cfo.diagnosis
            let drivers = Array(viewModel.drivers.prefix(5))
            let summary = Self.atStakeLine(cfo, driverCount: viewModel.drivers.count)
            let caveat = Self.caveatLine(dg)
            let whyKind = cfo.claimKinds?["why"]?.lowercased()
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if let caveat {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarAmber)
                            .accessibilityHidden(true)
                        Text(caveat).cavnarText(.caption, color: .cavnarAmber)
                    }
                    .accessibilityElement(children: .combine)
                }
                CavnarAnswerCard(
                    kicker: "Where the money is going",
                    headline: dg?.headline ?? summary ?? "What is driving food cost",
                    summary: dg?.headline == nil ? nil : summary,
                    cause: dg?.cause,
                    // The cause is Cavnar AI's read unless the server says
                    // it was measured or computed.
                    isHypothesis: dg?.cause != nil && whyKind != "measured" && whyKind != "computed",
                    alternativeCause: dg?.alternativeCause,
                    expectedOutcome: OwnerCopy.expectedOutcome(dg?.expectedOutcome).map { "If this is the cause: \($0)" },
                    confidence: dg?.cause == nil ? nil : dg?.trust.map {
                        ConfidenceLine(confidence: $0, recKey: dg?.recKey, surface: "food", module: "food")
                    },
                    detailLabel: "Show the full read",
                    surface: nil
                ) {
                    decisionActions(dg, top: drivers.first)
                } detail: {
                    fullRead(cfo, drivers: Array(drivers.dropFirst()))
                }
            }
            .cavnarCard(.ai)
        }
    }

    /// "$2,400 a month at stake across 3 drivers · read 10/7/26".
    private static func atStakeLine(_ cfo: FoodCostCFO, driverCount: Int) -> String? {
        guard let total = cfo.drivers?.atStake, total > 0 else { return nil }
        let dg = cfo.diagnosis
        return "$\(total.commaFormatted) a month at stake across \(driverCount) driver\(driverCount == 1 ? "" : "s")"
            + (dg?.asOf.map { " \u{00B7} read \($0)" } ?? "")
    }

    /// The caveats, compressed to one line ("Older read · 2 figures
    /// unverified · details in the full read"); the full sentences are in
    /// the full read.
    private static func caveatLine(_ dg: FoodCostCFO.Diagnosis?) -> String? {
        var parts: [String] = []
        if (dg?.staleNote?.isEmpty == false) || (dg?.stale ?? false) { parts.append("Older read") }
        if let n = dg?.unsupportedFigures?.count, n > 0 {
            parts.append("\(n) figure\(n == 1 ? "" : "s") unverified")
        }
        guard !parts.isEmpty else { return nil }
        return parts.joined(separator: " \u{00B7} ") + " \u{00B7} details in the full read"
    }

    /// Do this first and its answer row, then the top driver's dollars and
    /// where it is acted on.
    @ViewBuilder
    private func decisionActions(_ dg: FoodCostCFO.Diagnosis?, top: FoodCostCFO.Driver?) -> some View {
        // An answered action drops with its controls; the evidence stays.
        if let action = dg?.recommendedAction, dg?.answered != true {
            // An older read's action is what it suggested then — no answer
            // controls (memory round).
            let held = dg?.showsControls == false && dg?.recKey != nil
            (Text(held ? "It suggested then: " : "Do this first: ")
                .font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
             + HomeMixedText.make(action, role: .body, color: held ? .cavnarInk2 : .cavnarInk))
                .lineSpacing(CavnarText.body.lineSpacing)
                .fixedSize(horizontal: false, vertical: true)
            if dg?.showsControls == true, let key = dg?.recKey {
                RecAnswerRow(key: key, surface: "food")
            }
        }
        if let top {
            driverRow(top, compact: true)
                .padding(.top, CavnarSpace.xxs)
        }
    }

    /// Everything else the read holds, for the owner who wants the proof.
    @ViewBuilder
    private func fullRead(_ cfo: FoodCostCFO, drivers rest: [FoodCostCFO.Driver]) -> some View {
        let dg = cfo.diagnosis
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            // The server's own sentence for a read that hasn't been
            // refreshed — the same caveat the Reviews diagnosis carries.
            if let note = dg?.staleNote, !note.isEmpty {
                CavnarCaveat(title: "Older read", detail: note)
            }
            if let figs = dg?.unsupportedFigures, !figs.isEmpty {
                CavnarCaveat.unverifiedFigures(figs)
            }
            if let confirm = dg?.whatWouldConfirm { readRow("What would tell them apart", confirm) }
            if let oe = dg?.operationalEvidence, !oe.isEmpty {
                readRow("Checked against",
                        oe.map { "\($0.module.replacingOccurrences(of: "_", with: " ")): \($0.metric) \($0.value)" }
                          .joined(separator: "  \u{00B7}  "))
            }
            if let p = cfo.profitability, p.available,
               let prime = p.primeCostPct, let projected = p.projectedPrimeCost {
                profitabilityBlock(p, prime: prime, projected: projected,
                                   record: cfo.brief?.primeCostAccuracy?.line)
            }
            if !rest.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        CavnarKicker("Other drivers, biggest first")
                        Spacer(minLength: 0)
                        ClaimKindTag(kind: cfo.claimKinds?["drivers"])
                    }
                    ForEach(rest) { d in
                        driverRow(d, compact: false)
                    }
                }
            }
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

    /// "Corrected: near $41,200 (38.9%) — already corrected: earlier
    /// projections ran 12% high".
    static func correctionSentence(_ fix: ProjectionCorrection, line: String) -> String {
        var s = ""
        if let projected = fix.projectedPrimeCost {
            s = "Corrected: near $\(projected.commaFormatted)"
            if let pct = fix.primeCostPct { s += String(format: " (%.1f%%)", pct) }
            s += " \u{2014} "
        }
        return s + line
    }

    private func profitabilityBlock(_ p: FoodCostCFO.Profitability,
                                    prime: Double, projected: Double, record: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker("Prime cost")
            Text("\(prime, specifier: "%.1f")% prime cost").cavnarText(.figureS)
            // Built up in statements rather than one concatenated expression:
            // the single-expression version pushed the type checker past its
            // budget and failed the build outright.
            CavnarMixedText(Self.profitabilitySentence(p, projected: projected), role: .secondary)
            // The projection read against its own record (memory round):
            // when earlier month-ends leaned, the corrected figure and why.
            if let fix = p.projectionCorrection, let line = fix.line {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    Image(systemName: "scope")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .accessibilityHidden(true)
                    CavnarMixedText(Self.correctionSentence(fix, line: line), role: .secondary)
                }
            }
            if let labor = p.laborBasisText {
                CavnarMixedText("Labor: " + labor + ".", role: .caption)
            }
            if let basis = p.basis {
                Text("A projection from this month so far, not a measurement. \(basis)")
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // How past month-end projections held up here (K8), so this
            // one can be weighed — months, not weeks.
            if let record {
                CavnarMixedText(record + ".", role: .caption)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(CavnarSpace.s)
        .background(Color.cavnarEmber.opacity(0.09), in: RoundedRectangle(cornerRadius: 9))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Forecast. Prime cost \(String(format: "%.1f", prime)) percent month to date.")
    }

    /// "Easy fix" / "Some work" / "Bigger job" — the driver's difficulty.
    static func effortWords(_ difficulty: String) -> String {
        switch difficulty.lowercased() {
        case "low", "easy": return "Easy fix"
        case "medium": return "Some work"
        case "high", "hard": return "Bigger job"
        default: return difficulty.prefix(1).uppercased() + difficulty.dropFirst()
        }
    }

    /// One cost driver: its monthly dollars and label, its evidence, how
    /// sure, the effort and what it costs if ignored, and where to act.
    /// `compact` (the card's top driver) drops the evidence and the
    /// confidence line — the card already carries the read's.
    private func driverRow(_ d: FoodCostCFO.Driver, compact: Bool) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Text("$\(d.dollarsMonthly.commaFormatted)/mo").cavnarText(.figureS)
                Text(d.label)
                    .cavnarText(.label)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !compact {
                CavnarMixedText(d.evidence, role: .secondary)
                // How sure, on the shared line (K1).
                if let c = d.trust {
                    ConfidenceLine(confidence: c, recKey: d.recKey, surface: "food", module: "food")
                }
            }
            CavnarMixedText("\(Self.effortWords(d.difficulty)) \u{00B7} costs $\(d.dollarsMonthly.commaFormatted)/mo if ignored",
                            role: .caption)
            // Where it is acted on (U4-9), the server's own nav path — the
            // web's "Open the order →" (parity audit #76).
            if let act = d.act, let path = NavPath(act.nav), let action = FoodCostAction(path: path) {
                Button {
                    Haptic.light()
                    route(action)
                } label: {
                    HStack(spacing: CavnarSpace.xxs) {
                        Text(act.label)
                        Image(systemName: "arrow.right").accessibilityHidden(true)
                    }
                    .cavnarText(.label, color: .cavnarEmber2)
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // .contain, not .combine: the button inside must stay reachable.
        .accessibilityElement(children: .contain)
    }

    /// A driver's action: the pars are on this tab; anything else opens
    /// through the screen's own action sheet.
    private func route(_ action: FoodCostAction) {
        if case .pars = action {
            viewModel.scrollToPars = true
            return
        }
        open?(action)
    }

    private func readRow(_ label: String, _ body: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            Text(label).cavnarText(.label)
            CavnarMixedText(body, role: .secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label). \(body)")
    }

    // MARK: - Hero (the one real container on this page)

    // The AI strip is the LAST row inside this same VStack, after a
    // divider — one continuous surface with the position above it.
    private func heroCard(_ a: FoodCostAnalytics, isLoading: Bool) -> some View {
        let startFromZero = !viewModel.hasPlayedHeroIntro
        return VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: CavnarSpace.m) {
                positionContent(a)
                // Waste is the second figure: this week's measured waste
                // and the recoverable run-rate beside it — an OPPORTUNITY
                // (I8, `recoverable_kind`): amber "available", never a
                // win's green, and never summed with anything.
                if a.totalWasteCostWeek != nil || (a.recoverableMonthly ?? 0) > 0 {
                    HStack(alignment: .top, spacing: CavnarSpace.l) {
                        if let week = a.totalWasteCostWeek {
                            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                                Text("Waste this week").cavnarText(.secondary)
                                HeroAnimatedNumber(numericValue: week, tone: Color.cavnarRed,
                                                   startFromZero: startFromZero)
                            }
                        }
                        Spacer(minLength: CavnarSpace.s)
                        if (a.recoverableMonthly ?? 0) > 0 {
                            VStack(alignment: .trailing, spacing: CavnarSpace.xxs) {
                                Text("Recoverable a month").cavnarText(.secondary)
                                HeroAnimatedNumber(numericValue: a.recoverableMonthly ?? 0, tone: Color.cavnarAmber,
                                                   startFromZero: startFromZero)
                                Text("an opportunity, not savings").cavnarText(.caption, color: .cavnarInk2)
                            }
                        }
                    }
                }
            }
            .padding(CavnarSpace.l)

            Rectangle().fill(Color.cavnarEmber.opacity(0.35)).frame(height: 1)

            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
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
            .padding(.horizontal, CavnarSpace.l)
            .padding(.top, CavnarSpace.m)
            // The forecast ribbon straddles this card's bottom edge (see
            // .cavnarRibbonHeroAnchor() below) — matches Labor's own
            // identical fix so the ribbon's pill doesn't touch the AI strip.
            .padding(.bottom, CavnarSpace.l)
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

    /// Count-up-once hero number — same treatment and reasoning as
    /// LaborAnalyticsSection's LaborStatTile. A real drop shadow underneath
    /// (not just a glow) gives it something to sit on against the card's
    /// own warm gradient.
    private struct HeroAnimatedNumber: View {
        let numericValue: Double
        let tone: Color
        let startFromZero: Bool

        @State private var animatedValue: Double = 0

        var body: some View {
            CavnarAnimatableNumber(value: animatedValue, format: { "$\($0.commaFormatted)" })
                .font(.cavnar(.figureM))
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

    // MARK: - The two ledgers

    private func wasteLedger(_ analytics: FoodCostAnalytics) -> some View {
        WasteLedgerChart(
            kicker: "Top waste offenders", title: "Waste Ledger",
            // total_waste_cost_week covers every item; waste_items is only
            // those above their category tolerance, capped at six — the
            // fallback is the server's own flagged total.
            headline: "This week · $\(Int((analytics.wasteItemsTotal ?? analytics.wasteItems.reduce(0) { $0 + $1.wasteCost }).rounded()).formatted()) flagged",
            rows: analytics.wasteItems.map {
                WasteLedgerChart.Row(id: $0.id, name: $0.item, value: $0.wasteCost, detail: String(format: "%.0f%% waste", $0.wastePct))
            }
        )
    }

    private func overstockLedger(_ analytics: FoodCostAnalytics) -> some View {
        WasteLedgerChart(
            kicker: "Overstocked", title: "Tied-Up Capital",
            // The server's total over every overstocked item — the list
            // here is truncated to five.
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

    // MARK: - What to order

    /// The order list and the one action it leads to: "Send order to
    /// suppliers", opened through the screen's own order sheet
    /// (`open(.order)`). The second copy of the action row that used to sit
    /// here — Menu margins, Scan an invoice, Recipes, Count sheet, each with
    /// its own sheet state — is gone; the action row at the top has them.
    @ViewBuilder
    private func orderSection(_ a: FoodCostAnalytics) -> some View {
        if !a.criticalLow.isEmpty || !a.reorderSoon.isEmpty || !a.orderReduction.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.l) {
                sectionTitle("What to order")
                if !a.criticalLow.isEmpty {
                    actionGroup(title: "Order now", color: Color.cavnarRed, textColor: .cavnarRedText,
                                items: a.criticalLow, showDays: true)
                }
                if !a.reorderSoon.isEmpty {
                    actionGroup(title: "Order soon", color: Color.cavnarAmber, textColor: .cavnarAmber,
                                items: a.reorderSoon, showDays: true)
                }
                if !a.orderReduction.isEmpty {
                    actionGroup(title: "Order less", color: Color.cavnarGreen, textColor: .cavnarGreen,
                                items: a.orderReduction, showDays: false)
                }
                // The step that sends the list to the supplier who fills it.
                if !a.criticalLow.isEmpty || !a.reorderSoon.isEmpty {
                    Button {
                        Haptic.light()
                        open?(.order)
                    } label: {
                        HStack(spacing: CavnarSpace.xs) {
                            Image(systemName: "paperplane.fill").accessibilityHidden(true)
                            Text("Send order to suppliers")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: false))
                }
            }
        }
    }

    private static let orderRowsShown = 5

    private func actionGroup(title: String, color: Color, textColor: Color,
                             items: [InventoryActionItem], showDays: Bool) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: CavnarSpace.xs) {
                Text(title).cavnarText(.label, color: textColor)
                Spacer()
                Text("\(items.count)").cavnarText(.figureS, color: textColor)
            }
            .accessibilityElement(children: .combine)
            VStack(spacing: 0) {
                ForEach(Array(items.prefix(Self.orderRowsShown).enumerated()), id: \.element.id) { index, item in
                    if index > 0 { hairline }
                    actionRow(item, color: color, showDays: showDays)
                }
            }
            if items.count > Self.orderRowsShown {
                CavnarMoreDisclosure(hiddenCount: items.count - Self.orderRowsShown) {
                    VStack(spacing: 0) {
                        ForEach(Array(items.dropFirst(Self.orderRowsShown))) { item in
                            hairline
                            actionRow(item, color: color, showDays: showDays)
                        }
                    }
                }
            }
        }
    }

    private var hairline: some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }

    private func actionRow(_ item: InventoryActionItem, color: Color, showDays: Bool) -> some View {
        HStack(spacing: CavnarSpace.s) {
            Rectangle().fill(color).frame(width: 2.5)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.item).cavnarText(.label)
                CavnarMixedText(subtitle(for: item, showDays: showDays), role: .secondary)
            }
            Spacer(minLength: CavnarSpace.xs)
            VStack(alignment: .trailing, spacing: 2) {
                Text(Self.sentenceCase(item.orderCaption)).cavnarText(.caption)
                Text(item.suggestedOrderLabel).cavnarText(.figureS)
                // A per-order difference against the last order, not money
                // saved: neutral ink, and it says what it is compared with.
                if let delta = item.savingsVsLast, delta != 0 {
                    CavnarMixedText(delta > 0 ? "↓ $\(String(format: "%.2f", delta)) vs last order"
                                              : "↑ $\(String(format: "%.2f", -delta)) vs last order",
                                    role: .caption)
                }
            }
        }
        .padding(.vertical, CavnarSpace.s)
        .accessibilityElement(children: .combine)
    }

    /// "ORDER 4 MORE" → "Order 4 more".
    static func sentenceCase(_ s: String) -> String {
        let lower = s.lowercased()
        return lower.prefix(1).uppercased() + lower.dropFirst()
    }

    /// "2 days left · last ordered 12 lb" — the number is always a past
    /// ORDER quantity, never a stock level.
    private func subtitle(for item: InventoryActionItem, showDays: Bool) -> String {
        let unitSuffix = item.unit.map { " \($0)" } ?? ""
        let last = item.lastOrderQty.map {
            "Last ordered " + ($0.truncatingRemainder(dividingBy: 1) == 0 ? "\(Int($0))" : String(format: "%.1f", $0)) + unitSuffix
        } ?? "No order on file"
        if showDays, let days = item.daysRemaining {
            let daysStr = days.truncatingRemainder(dividingBy: 1) == 0 ? "\(Int(days))" : String(format: "%.1f", days)
            return "\(daysStr) day\(days == 1 ? "" : "s") left \u{00B7} " + last.prefix(1).lowercased() + last.dropFirst()
        }
        return last
    }

    // MARK: - Pars to raise

    private static func price(_ v: Double) -> String { String(format: "$%.2f", v) }

    /// An item the close-out ran out of on two or more nights in four
    /// weeks, with the par that would have covered it. Never written until
    /// the owner taps Raise par.
    private func parSection(_ items: [ParSuggestion]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            sectionTitle("Pars to raise")
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { hairline }
                    parRow(item)
                }
            }
            Text("From the close-out\u{2019}s 86 list. Raising a par changes what the order draft suggests.")
                .cavnarText(.caption)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func parRow(_ s: ParSuggestion) -> some View {
        let raised = viewModel.parRaised[s.ingredientId]
        let dismissed = viewModel.parDismissed.contains(s.ingredientId)
        let busy = viewModel.parBusy.contains(s.ingredientId)
        return HStack(alignment: .top, spacing: CavnarSpace.s) {
            Rectangle().fill(Color.cavnarAmber).frame(width: 2.5)
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                HStack(alignment: .firstTextBaseline) {
                    Text(s.name).cavnarText(.label)
                    Spacer(minLength: CavnarSpace.xs)
                    (Text(ParSuggestion.qty(s.par)).foregroundStyle(Color.cavnarInk2)
                     + Text("  \u{2192}  ").foregroundStyle(Color.cavnarInk2)
                     + Text(ParSuggestion.qty(s.suggestedPar)).foregroundStyle(Color.cavnarInk))
                        .font(.cavnar(.figureS))
                        .accessibilityLabel("Par now \(ParSuggestion.qty(s.par)), suggested \(ParSuggestion.qty(s.suggestedPar))")
                }
                if let why = s.why {
                    CavnarMixedText(why, role: .secondary)
                }
                if let raised {
                    HStack(spacing: CavnarSpace.xxs) {
                        Image(systemName: "checkmark").accessibilityHidden(true)
                        CavnarMixedText("Par set to \(ParSuggestion.qty(raised))", role: .secondary)
                    }
                    .foregroundStyle(Color.cavnarInk2)
                    .padding(.top, CavnarSpace.xxs)
                } else {
                    HStack(alignment: .center, spacing: CavnarSpace.m) {
                        if !dismissed, s.suggestedPar != nil {
                            Button {
                                Haptic.light()
                                Task { await viewModel.acceptPar(s) }
                            } label: {
                                if busy { CavnarShimmerText(text: "Setting\u{2026}") } else { Text("Raise par") }
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
                            .disabled(busy)
                        }
                        if let key = s.key {
                            RecAnswerRow(key: key, surface: "food", answers: [.notForUs],
                                         onAnswered: { _ in viewModel.parDismissed.insert(s.ingredientId) })
                                .disabled(busy)
                        }
                    }
                    .padding(.top, CavnarSpace.xxs)
                    if let error = viewModel.parErrors[s.ingredientId] {
                        Text(error)
                            .cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .padding(.vertical, CavnarSpace.s)
    }

    // MARK: - Dishes to reprice
    //
    // The one reprice list on the phone: the dish, what the rise costs a
    // month, now → suggested, one Set button and Pass. Why it moved, the
    // basis of the monthly figure, what this owner usually picks (and a tap
    // to set it) and what guests said about its value are behind "Why".
    // Every dish's margin, the uncosted and unmatched lists and the
    // scorecard's numbers are on the web.

    /// The one-tap reprice: primary, or secondary under a guard.
    private struct RepriceButtonStyle: ButtonStyle {
        let guarded: Bool
        let isDisabled: Bool
        @ViewBuilder
        func makeBody(configuration: Configuration) -> some View {
            if guarded {
                CavnarSecondaryButtonStyle(isDisabled: isDisabled).makeBody(configuration: configuration)
            } else {
                CavnarPrimaryButtonStyle(isDisabled: isDisabled).makeBody(configuration: configuration)
            }
        }
    }

    private func repriceSection(_ items: [RepriceSuggestions.Suggestion], assumption: String?) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            sectionTitle("Dishes to reprice", count: items.count + viewModel.scorecardReprice.count)
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { hairline }
                    repriceRow(item)
                }
                // The scorecard's reprice moves with no suggested price:
                // the price is the owner's to choose, in Menu margins.
                ForEach(Array(viewModel.scorecardReprice.enumerated()), id: \.element.id) { index, dish in
                    if index > 0 || !items.isEmpty { hairline }
                    scorecardRepriceRow(dish)
                }
            }
            if let assumption, !assumption.isEmpty {
                Text(assumption)
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button {
                Haptic.light()
                open?(.dishes)
            } label: {
                HStack(spacing: CavnarSpace.xxs) {
                    Text("All your dishes")
                    Image(systemName: "chevron.right").accessibilityHidden(true)
                }
                .cavnarText(.label, color: .cavnarEmber2)
                .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            CavnarWebLinkRow(title: "Every dish\u{2019}s margin", subtitle: "Plate costs, dishes with no recipe, unmatched items",
                             path: "inventory/margins", actionLabel: "Open on the web")
        }
    }

    private func repriceRow(_ s: RepriceSuggestions.Suggestion) -> some View {
        let applied = viewModel.repriceApplied[s.dish]
        let dismissed = viewModel.repriceDismissed.contains(s.dish)
        let busy = viewModel.repriceBusy.contains(s.dish)
        let whyOpen = repriceWhyOpen.contains(s.dish)
        let hasWhy = s.whyLine != nil || s.monthlyBasis != nil || s.typicalLine != nil || s.valueNote != nil
            || s.typicalPrice != nil
        return HStack(alignment: .top, spacing: CavnarSpace.s) {
            Rectangle().fill(Color.cavnarAmber).frame(width: 2.5)
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                HStack(alignment: .firstTextBaseline) {
                    Text(s.dish)
                        .cavnarText(.label)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: CavnarSpace.xs)
                    // Dollars a month when there is a sales mix; the
                    // per-plate figure when there isn't — never a $0.
                    if let monthly = s.monthlyMarginLost {
                        Text("$\(monthly.commaFormatted)/mo").cavnarText(.figureS, color: .cavnarRedText)
                    } else if let perPlate = s.increasePerPlate {
                        Text("+\(Self.price(perPlate))/plate").cavnarText(.figureS, color: .cavnarRedText)
                    }
                }
                if let now = s.sellPrice, let suggested = s.suggestedPrice {
                    (Text("Now ").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                     + Text(Self.price(now)).font(.cavnar(.figureS)).foregroundStyle(Color.cavnarInk2)
                     + Text("  \u{2192}  ").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                     + Text(Self.price(suggested)).font(.cavnar(.figureS)).foregroundStyle(Color.cavnarInk))
                        .accessibilityLabel("Now \(Self.price(now)), suggested \(Self.price(suggested))")
                }
                // A live link says fix the plate before the price: the
                // guard's words, and the one-tap button steps down.
                if let g = s.repriceGuard {
                    CavnarCaveat(title: "Fix the plate before the price", detail: g.text)
                }

                if let applied {
                    HStack(spacing: CavnarSpace.xxs) {
                        Image(systemName: "checkmark").accessibilityHidden(true)
                        CavnarMixedText("Set to \(Self.price(applied))", role: .secondary)
                    }
                    .foregroundStyle(Color.cavnarInk2)
                    if let tracking = viewModel.repriceTracking[s.dish] {
                        RecTrackerLine(text: tracking)
                    }
                } else {
                    HStack(alignment: .center, spacing: CavnarSpace.m) {
                        if let suggested = s.suggestedPrice, !dismissed {
                            Button {
                                Task { await viewModel.applyReprice(s) }
                            } label: {
                                if busy {
                                    CavnarShimmerText(text: "Setting\u{2026}")
                                } else {
                                    (Text(s.repriceGuard == nil ? "Set " : "Set anyway: ")
                                     + Text(Self.price(suggested)).font(.cavnar(.figureS)))
                                }
                            }
                            // Demoted under a guard (memory round, "links").
                            .buttonStyle(RepriceButtonStyle(guarded: s.repriceGuard != nil, isDisabled: busy))
                            .disabled(busy)
                            .accessibilityHint("Changes \(s.dish)'s menu price")
                        }
                        if let key = s.recKey {
                            RecAnswerRow(key: key, surface: "food", answers: [.notForUs],
                                         onAnswered: { _ in viewModel.repriceDismissed.insert(s.dish) })
                                .disabled(busy)
                        }
                    }
                    if let error = viewModel.repriceErrors[s.dish] {
                        Text(error)
                            .cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                if hasWhy {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.2)) {
                            if whyOpen { repriceWhyOpen.remove(s.dish) } else { repriceWhyOpen.insert(s.dish) }
                        }
                    } label: {
                        HStack(spacing: CavnarSpace.xxs) {
                            Text(whyOpen ? "Hide why" : "Why")
                            Image(systemName: "chevron.down")
                                .rotationEffect(.degrees(whyOpen ? 180 : 0))
                                .accessibilityHidden(true)
                        }
                        .cavnarText(.label, color: .cavnarEmber2)
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityValue(whyOpen ? "Expanded" : "Collapsed")
                    if whyOpen {
                        repriceWhy(s, dismissed: dismissed, busy: busy, applied: applied != nil)
                            .transition(.opacity)
                    }
                }
            }
        }
        .padding(.vertical, CavnarSpace.s)
    }

    /// A dish the scorecard says to reprice, with no suggested price: why,
    /// its price and food cost, and one tap to its price in Menu margins.
    private func scorecardRepriceRow(_ dish: DishScorecard.Dish) -> some View {
        HStack(alignment: .top, spacing: CavnarSpace.s) {
            Rectangle().fill(Color.cavnarAmber).frame(width: 2.5)
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                Text(dish.name).cavnarText(.label).fixedSize(horizontal: false, vertical: true)
                if let why = dish.why {
                    CavnarMixedText(why, role: .secondary)
                }
                CavnarMixedText(DishScorecardSheet.priceLine(dish), role: .caption)
                Button {
                    Haptic.light()
                    open?(.menu(dish: dish.name))
                } label: {
                    Text("Set a price").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .accessibilityHint("Opens \(dish.name)'s menu price")
            }
        }
        .padding(.vertical, CavnarSpace.s)
    }

    /// Why the dish moved, what the monthly figure rests on, what this owner
    /// usually picks (one tap to set it) and what guests said about value.
    private func repriceWhy(_ s: RepriceSuggestions.Suggestion, dismissed: Bool, busy: Bool,
                            applied: Bool) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if let why = s.whyLine {
                CavnarMixedText(why, role: .secondary)
            }
            if s.monthlyMarginLost != nil, let basis = s.monthlyBasis {
                Text("Margin lost a month, from \(basis).")
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // What this owner usually does with a reprice, and what that
            // recovers (memory round: owner_ratio / typical_price).
            if let typical = s.typicalLine {
                CavnarMixedText(typical + ".", role: .secondary)
            }
            // Guests calling the dish poor value (M1 value_note).
            if let value = s.valueNote {
                CavnarMixedText(value + ".", role: .secondary, color: .cavnarAmber)
            }
            // The owner's usual price, one tap (typical_price).
            if let typical = s.typicalPrice, !dismissed, !busy, !applied, typical != s.suggestedPrice {
                Button {
                    Haptic.light()
                    Task { await viewModel.applyReprice(s, price: typical) }
                } label: {
                    (Text("Set ") + Text(Self.price(typical)).font(.cavnar(.figureS)))
                        .cavnarText(.label, color: .cavnarEmber2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityHint("Sets \(s.dish) to the price you usually choose")
            }
        }
        .padding(.leading, CavnarSpace.xxs)
    }

    // MARK: - Price watch

    private static let priceWatchShown = 3

    private func priceWatchDetail(_ items: [PriceWatchItem]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Price watch")
            VStack(spacing: 0) {
                ForEach(Array(items.prefix(Self.priceWatchShown).enumerated()), id: \.element.id) { index, item in
                    if index > 0 { hairline }
                    priceWatchRow(item)
                }
            }
            if items.count > Self.priceWatchShown {
                CavnarMoreDisclosure(hiddenCount: items.count - Self.priceWatchShown) {
                    VStack(spacing: 0) {
                        ForEach(Array(items.dropFirst(Self.priceWatchShown))) { item in
                            hairline
                            priceWatchRow(item)
                        }
                    }
                }
            }
        }
    }

    private func priceWatchRow(_ item: PriceWatchItem) -> some View {
        let accent = item.isTrend ? Color.cavnarRed : Color.cavnarAmber
        return HStack(alignment: .top, spacing: CavnarSpace.s) {
            Rectangle().fill(accent).frame(width: 2.5)
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                Text(item.item).cavnarText(.label)
                Text("$\(String(format: "%.2f", item.oldPrice)) → $\(String(format: "%.2f", item.newPrice))")
                    .cavnarText(.figureS, color: .cavnarInk2)
                Text(item.actionHint)
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: CavnarSpace.xs)
            VStack(alignment: .trailing, spacing: 2) {
                // Sign from the value — a drop would have rendered "+-6%".
                Text("\(item.changePct >= 0 ? "+" : "")\(String(format: "%.0f", item.changePct))%")
                    .cavnarText(.figureS, color: item.isTrend ? .cavnarRedText : .cavnarAmber)
                Text(item.timeframeLabel).cavnarText(.caption)
            }
        }
        .padding(.vertical, CavnarSpace.s)
    }
}

// The forecast pill mimics Labor's exactly via the shared
// DesignSystem/HeroForecastRibbon.swift component — see
// FoodCostQuickEntryView's .cavnarHeroForecastRibbon(...) call and this
// file's heroCard .cavnarRibbonHeroAnchor().
