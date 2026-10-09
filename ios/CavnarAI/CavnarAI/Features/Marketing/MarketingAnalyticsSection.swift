import SwiftUI

/// The Analytics tab, top to bottom (readability round 10/8/26): the brief
/// (one headline, the numbered moves, ONE forecast) with its caveats folded
/// into one line, the guest texts' most likely cause, the period, the stats
/// tile with the engagement-rate ring, thin platform bars, the top post, and
/// how you compare. What posts did to sales — a table of before/after rows
/// by kind, occasion and dish — is the web's, one tap away; the output
/// counts and the per-piece history are too.
struct MarketingAnalyticsSection: View {
    let viewModel: MarketingAnalyticsViewModel

    /// The caveat banners behind the one-line summary.
    @State private var showingCaveats = false
    /// The stats tile, the platform bars and the top post, behind "See the
    /// numbers" — the outcome line stays on top (re-audit 10/8/26 W9).
    @State private var showingNumbers = false
    /// The ring and the platform labels grow with the text size (L13).
    @ScaledMetric(relativeTo: .body) private var ringSize: CGFloat = 78

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            briefCard
            // Under the brief, as on the web: the guest texts' most likely
            // cause. Renders nothing until two campaigns can be compared.
            if let diagnosis = viewModel.diagnosis {
                LaborDiagnosisCard(diagnosis: diagnosis, title: "WHY SOME TEXTS DID BETTER", surface: "marketing")
            }

            if viewModel.isLoading && viewModel.performance == nil {
                // Analyzing performance/attribution across several requests
                // at once — the app's real "working" moment, not a plain
                // shimmering placeholder line.
                CavnarWorkingOrb(state: .solving, label: "Analyzing your marketing…")
                    .padding(.vertical, 24)
                    .frame(maxWidth: .infinity)
            } else {
                CachedDataNotice(text: viewModel.stalenessNotice)
                periodSwitcher
                // A period that couldn't load says so; the figures on
                // screen stay (L17).
                if let error = viewModel.windowError {
                    Text(error)
                        .cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // "Metrics synced 9/21/26" — only when it is news: amber
                // when the nightly pull is stale or failing (DH4-8), so a
                // flat week reads as what it is.
                if let sync = viewModel.performance?.metricsSync, sync.tone == "warn" || sync.tone == "bad" {
                    ServerStatusCaption(status: sync)
                }
                if let window = viewModel.window {
                    // The outcome, in one line: reach against the period
                    // before, toned — the Content tab's own rule.
                    let outcome = MarketingView.outcome(window)
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        HomeMixedText.make(outcome.headline, role: .figureM, color: .cavnarInk, numberColor: .cavnarInk)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                        if let change = outcome.change {
                            CavnarMixedText(change, role: .secondary, color: outcome.tone)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityElement(children: .combine)
                    numbersToggle
                    if showingNumbers {
                        statsTile(window)
                        // Why there's no +/−% beside the figures (F2) — a
                        // blank change is "too few posts", never "no change".
                        if let note = window.changeNote, !note.isEmpty {
                            CavnarMixedText(note + ".", role: .caption)
                        }
                        if !window.byPlatform.isEmpty {
                            platformBars(window)
                        }
                        topPostCard
                    }
                } else if viewModel.performance != nil {
                    Text("No published post metrics yet.")
                        .cavnarText(.body)
                        .cavnarCard()
                    topPostCard
                }
                // What a post did to the till — same weekday, before vs
                // after, by kind, occasion and dish — on the web (#60's
                // rule: wide tables and multi-row analysis are the web's).
                if viewModel.attribution != nil {
                    // marketing/attribution lands on the web's Analytics tab
                    // at the card itself (re-audit 10/8/26 M6) — "marketing"
                    // opened Content.
                    CavnarWebLinkRow(title: "What posts did to sales",
                                     subtitle: "Same weekday, before vs after \u{2014} by post, kind, occasion and dish",
                                     path: "marketing/attribution", actionLabel: "Open on the web")
                }
                // How you compare — the Benchmark Engine's card (#23).
                HowYouCompareCard(module: "marketing")
            }
        }
    }

    // MARK: - Brief

    /// One line for every caveat on the brief — nil when there is none
    /// (the same summary Reviews' read uses).
    private func caveatLine(_ insight: AIInsight) -> String? {
        ReviewsAnalyticsSection.caveatSummary(
            unverified: insight.figuresVerified == false || insight.causesVerified == false,
            stale: insight.olderReadNote != nil)
    }

    /// The consultant's read as one headline and the numbered moves — not
    /// a strip of prose. The insight endpoint already writes in this shape
    /// (Line 1, then "1." "2."); this just stops flattening it back out.
    private var briefCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            // A brief the server could not write, in its own words
            // (InsightRefresh.follow, re-audit 10/8/26 #3).
            if let message = viewModel.insightError {
                CavnarCaveat.readUnavailable(message)
                    .padding(.bottom, 10)
            }
            if viewModel.isLoadingInsight && viewModel.insight == nil {
                CavnarWorkingOrb(state: .solving, label: "Reading your numbers…")
                    .padding(.vertical, 8)
                    .frame(maxWidth: .infinity)
            } else if let insight = viewModel.insight {
                // Up to three caveat banners stood above the read; they are
                // one line now, the banners behind "Why?".
                if let line = caveatLine(insight) {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.2)) { showingCaveats.toggle() }
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Image(systemName: "exclamationmark.circle")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarAmber)
                            Text(line)
                                .cavnarText(.caption, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                            Text(showingCaveats ? "Hide" : "Why?")
                                .font(.cavnarBody(CavnarType.caption, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                            Spacer(minLength: 0)
                        }
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if showingCaveats {
                        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                            // A figure the server couldn't trace to the data:
                            // said, and the lines carry no Done / Track (M-16).
                            if insight.figuresVerified == false {
                                CavnarCaveat.unverifiedFigures(insight.unsupportedFigures ?? [])
                            }
                            // A reason the read gave that nothing measured backs (H2).
                            if insight.causesVerified == false {
                                CavnarCaveat.unverifiedCauses(insight.unsupportedCauses ?? [])
                            }
                            // A cached read served because a new one failed (B6 sub-audit).
                            if let note = insight.olderReadNote {
                                CavnarCaveat.olderRead(note)
                            }
                        }
                        .padding(.bottom, 10)
                    }
                }
                CavnarMixedText(insight.intro, role: .lead)
                    .padding(.bottom, insight.recommendations.isEmpty ? 0 : 12)

                // The top move, then the rest behind "+N more" (re-audit
                // 10/8/26 M17). The brief carries no per-move confidence or
                // outcome, so none is shown — never one invented.
                if let first = insight.recommendations.first {
                    moveRow(first, index: 0, insight: insight, lead: insight.recommendations.count > 1)
                }
                if insight.recommendations.count > 1 {
                    CavnarMoreDisclosure(hiddenCount: insight.recommendations.count - 1) {
                        ForEach(Array(insight.recommendations.enumerated().dropFirst()), id: \.offset) { index, rec in
                            moveRow(rec, index: index, insight: insight, lead: false)
                        }
                    }
                }

                // ONE forecast: the figure computed in Python (H8) — last
                // week's level carried forward, never extrapolated — when
                // there is one, else the brief's own line; tagged either way.
                if let forecast = insight.computedForecast?.line ?? insight.forecast, !forecast.isEmpty {
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        ClaimKindTag(kind: "forecast")
                        CavnarMixedText(forecast, role: .secondary)
                    }
                    .padding(.top, 8)
                    .accessibilityElement(children: .combine)
                }
            } else {
                Text("Publish a post or two and the brief will have something to say.")
                    .cavnarText(.body)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(CavnarSpace.cardPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarEmber.opacity(0.12))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarEmber.opacity(0.35), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }

    /// One move of the brief, numbered, with its Done / Not for us / Track
    /// for a line the server keyed (insight_rec_keys, index-aligned). The
    /// first, when others follow, is "Do this first".
    private func moveRow(_ rec: String, index: Int, insight: AIInsight, lead: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("\(index + 1)")
                .cavnarText(.figureS, color: .cavnarEmber2)
            VStack(alignment: .leading, spacing: 6) {
                if lead {
                    CavnarKicker("Do this first")
                }
                CavnarMixedText(rec, role: .body)
                if let key = insight.recKey(at: index) {
                    RecAnswerRow(key: key, surface: "marketing")
                }
            }
        }
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// "See the numbers" — the tile, the platforms and the top post (W9).
    private var numbersToggle: some View {
        Button {
            Haptic.light()
            withAnimation(.cavnarEase(0.22)) { showingNumbers.toggle() }
        } label: {
            HStack(spacing: CavnarSpace.xxs + 2) {
                Text(showingNumbers ? "Hide the numbers" : "See the numbers")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                Image(systemName: "chevron.down")
                    .font(.cavnar(.caption))
                    .rotationEffect(.degrees(showingNumbers ? 180 : 0))
                    .accessibilityHidden(true)
                Spacer(minLength: 0)
            }
            .foregroundStyle(Color.cavnarEmber2)
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityValue(showingNumbers ? "Expanded" : "Collapsed")
    }

    // MARK: - Period

    private var periodSwitcher: some View {
        HStack(spacing: 0) {
            ForEach([7, 30, 90], id: \.self) { days in
                Button {
                    // The haptic fires here on the tap, not off the published
                    // value: the reload is async and a feedback modifier keyed
                    // to windowDays fired late (or not at all on failure).
                    guard days != viewModel.windowDays else { return }
                    Haptic.light()
                    Task { await viewModel.setWindow(days) }
                } label: {
                    HomeMixedText.make("\(days) days", role: .label,
                                       color: days == viewModel.windowDays ? .cavnarInk : .cavnarInk2)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(days == viewModel.windowDays ? Color.cavnarPaper3 : Color.clear)
                        .clipShape(Capsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(3)
        .background(Color.white.opacity(0.04))
        .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .clipShape(Capsule())
        .animation(.easeOut(duration: 0.2), value: viewModel.windowDays)
    }

    // MARK: - Stats

    /// "Top performing post" — named only over the floor of measured posts
    /// (AUX-13); below it, when one will be named. The web's
    /// `#mkt-perf-top-wrap`.
    @ViewBuilder
    private var topPostCard: some View {
        if let p = viewModel.performance {
            if let title = p.topPostTitle {
                VStack(alignment: .leading, spacing: 6) {
                    CavnarKicker("Top performing post")
                    Text(title)
                        .cavnarText(.label)
                        .fixedSize(horizontal: false, vertical: true)
                    if let metrics = p.topPostMetrics {
                        CavnarMixedText(metrics, role: .caption)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard()
            } else if let wait = p.topPostWaitLine {
                CavnarMixedText(wait, role: .caption)
            }
        }
    }

    /// A period against the period before it. "Total reach 4,231" with no
    /// denominator and no trend is a number, not a metric — and engagement
    /// RATE is the one that survives a follower count changing.
    private func statsTile(_ window: MarketingWindow) -> some View {
        HStack(alignment: .center, spacing: 8) {
            bigStat(window.reach.formatted(), "Reach", window.change.reach)
            bigStat(window.engagement.formatted(), "Engagement", window.change.engagement)
            rateRing(window.engagementRate)
                .frame(minWidth: ringSize + 18)
        }
        .cavnarGlossyCard()
    }

    private func bigStat(_ value: String, _ label: String, _ change: Double?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value)
                .cavnarText(.figureM)
                .cavnarNumberGlow()
                .lineLimit(1)
                .minimumScaleFactor(0.85)
            Text(label).cavnarText(.caption)
            if let change {
                Text("\(change > 0 ? "+" : "")\(change, specifier: "%.0f")%")
                    .font(.cavnarNumber(CavnarType.caption, weight: 700))
                    .foregroundStyle(change >= 0 ? Color.cavnarGreen : Color.cavnarRedText)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Engagement rate as a ring. Drawn against a 10% full scale — social
    /// rates live between 1% and 6%, and against 100% every ring would be a
    /// sliver that says nothing.
    private func rateRing(_ measured: Double?) -> some View {
        let rate = measured ?? 0
        return ZStack {
            Circle().stroke(Color.cavnarPaper3, lineWidth: 5)
            Circle()
                .trim(from: 0, to: min(max(rate / 10, 0), 1))
                .stroke(Color.cavnarEmber, style: StrokeStyle(lineWidth: 5, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(.easeOut(duration: 0.6), value: rate)
            VStack(spacing: 1) {
                // No reach measured is "—", never a 0.0% that reads as
                // "nobody engaged".
                Text(measured.map { String(format: "%.1f%%", $0) } ?? "\u{2014}")
                    .cavnarText(.figureS)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                Text("Rate").cavnarText(.caption)
            }
        }
        .frame(width: ringSize, height: ringSize)
        .padding(.vertical, 5)
    }

    // MARK: - Platforms

    private func platformBars(_ window: MarketingWindow) -> some View {
        let maxReach = max(window.byPlatform.map(\.reach).max() ?? 0, 1)
        return VStack(alignment: .leading, spacing: 10) {
            Text("By platform")
                .cavnarText(.label)

            ForEach(window.byPlatform) { platform in
                HStack(spacing: 10) {
                    // Sized by its words, so a long name or a large text
                    // size isn't cut to "Instag…" (L13); the bar gives way.
                    Text(platform.label)
                        .cavnarText(.secondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
                        .fixedSize(horizontal: true, vertical: false)
                        .frame(minWidth: 72, alignment: .leading)
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.cavnarPaper3)
                            Capsule()
                                .fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2],
                                                     startPoint: .leading, endPoint: .trailing))
                                .frame(width: geo.size.width * CGFloat(platform.reach) / CGFloat(maxReach))
                        }
                    }
                    .frame(height: 8)
                    Text(platform.reach > 0 ? platform.reach.formatted() : "—")
                        .font(.cavnarNumber(CavnarType.secondary, weight: 500))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: true, vertical: false)
                        .frame(minWidth: 44, alignment: .trailing)
                }
            }
        }
        .cavnarCard()
    }
}
