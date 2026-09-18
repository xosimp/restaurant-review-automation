import SwiftUI

struct ReviewsAnalyticsSection: View {
    let viewModel: ReviewsAnalyticsViewModel

    @State private var selectedTopic: TopicWeekRow?

    // Each section shows its own skeleton while it's individually still in
    // flight rather than gating the whole page behind one spinner — the 5
    // requests in ReviewsAnalyticsViewModel.load() run concurrently but are
    // awaited (and so become non-nil) in a fixed order, so e.g. the AI
    // insight — the slowest, since it's an LLM call, unlike the plain SQL
    // aggregates behind the other sections — used to visibly "pop in" a
    // couple seconds after everything else had already rendered.
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Picker("Period", selection: Binding(
                    get: { viewModel.windowDays },
                    set: { days in
                        guard days != viewModel.windowDays else { return }
                        Haptic.light()
                        Task { await viewModel.setWindow(days) }
                    })) {
                    Text("30 days").tag(30)
                    Text("90 days").tag(90)
                    Text("6 months").tag(180)
                }
                .pickerStyle(.segmented)

                // Raised above the analytics when the backend could not tie
                // every figure in the AI passage back to this restaurant's
                // own data — see ai_guard.verify_figures.
                if !viewModel.unsupportedFigures.isEmpty {
                    CavnarCaveat.unverifiedFigures(viewModel.unsupportedFigures)
                }
                if !viewModel.unsupportedNames.isEmpty {
                    CavnarCaveat.unverifiedNames(viewModel.unsupportedNames)
                }
                if viewModel.insightIsStale {
                    CavnarCaveat.olderRead(asOf: viewModel.insightAsOf)
                }

                // The AI read itself. It was fetched on every open and then
                // never rendered — so the app paid for a Haiku call, threw
                // the answer away, and could still show the caveat above
                // referring to a passage that was not on screen.
                if let insight = viewModel.insight, !insight.isEmpty {
                    insightCard(insight)
                    // Open complaints ranked by how serious they are rather
                    // than how many there are. Sits directly under the read
                    // because it is the same question that read is answering.
                    if !viewModel.severityTiers.isEmpty {
                        severityStrip(viewModel.severityTiers)
                    }
                } else if viewModel.isLoading {
                    insightSkeleton
                }

                // The root-cause card. Everything above it is a snapshot —
                // what happened and what to do next. This is the step after
                // that, and it renders only when a diagnosis actually exists:
                // an owner whose reviews do not yet support a cause sees
                // nothing here, never a cause produced to fill the space.
                if let diagnosis = viewModel.diagnosis {
                    diagnosisCard(diagnosis, money: viewModel.revenueAtRisk)
                }

                if let performance = viewModel.performance {
                    ResponseRingsChart(performance: performance)
                } else if viewModel.isLoading {
                    performanceSkeleton
                }

                // The weekly grid needs categorised reviews inside the last 8
                // weeks; the period-total cards stay as the fallback.
                if let topicWeeks = viewModel.topicWeeks, !topicWeeks.topics.isEmpty {
                    TopicHeatGridChart(
                        data: topicWeeks,
                        trends: Dictionary(uniqueKeysWithValues: viewModel.heatmap.map { ($0.category, $0.trend) })
                    ) { row in
                        selectedTopic = row
                    }
                } else if !viewModel.heatmap.isEmpty {
                    topicGrid
                } else if viewModel.isLoading {
                    topicGridSkeleton
                }

                if !viewModel.sentimentWeeks.isEmpty {
                    SentimentRiverChart(weeks: viewModel.sentimentWeeks)
                } else if viewModel.isLoading {
                    trendChartSkeleton
                }
            }
            .padding(20)
        }
        .navigationDestination(item: $selectedTopic) { topic in
            FilteredReviewsView(title: topic.label, category: topic.category)
        }
    }

    // MARK: - The AI read

    /// The endpoint writes 3-4 prefixed lines (📊 this week / ⚠️ watch /
    /// ✅ do today / 🔮 next week). Each becomes its own row with a symbol,
    /// and the forecast gets a separate tinted block — the same separation
    /// the web makes, because a prediction sitting in the same visual
    /// weight as a measured figure reads as another measured figure.
    private func insightCard(_ insight: String) -> some View {
        let lines = insight
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        return VStack(alignment: .leading, spacing: 12) {
            Text("Cavnar AI's read on your reviews")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.1)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarEmber)
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                let parsed = Self.parseInsightLine(line)
                if parsed.isForecast {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Forecast")
                            .font(.cavnarBody(9.5, weight: 700))
                            .tracking(0.8)
                            .textCase(.uppercase)
                            .foregroundStyle(Color.cavnarEmber)
                        Text(parsed.text)
                            .font(.cavnarBody(14.5))
                            .italic()
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, 10)
                    .padding(.vertical, 6)
                    .overlay(alignment: .leading) {
                        Rectangle().fill(Color.cavnarEmber).frame(width: 2)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Forecast. \(parsed.text)")
                } else {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Image(systemName: parsed.symbol)
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(parsed.tint)
                            .accessibilityHidden(true)
                        Text(parsed.text)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk2)
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(parsed.text)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color.cavnarEmber.opacity(0.09))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    // MARK: - Severity

    /// Open complaints by tier. Urgency answers "should this have woken the
    /// owner up?"; this answers "what kind of problem is it?", which is the
    /// question that orders twelve open complaints on a Tuesday morning.
    private func severityStrip(_ tiers: [SeverityTier]) -> some View {
        // Horizontally scrolling rather than wrapping: there are at most five
        // tiers and usually one or two, so this never actually scrolls — but
        // it also cannot clip a long label on a narrow phone, which a fixed
        // HStack would.
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(tiers) { tier in
                    HStack(spacing: 6) {
                        Text("\(tier.open)")
                            .font(.cavnarNumber(12.5, weight: 700))
                        Text(tier.label)
                            .font(.cavnarBody(11.5))
                    }
                    .foregroundStyle(Self.severityTint(tier.key))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Self.severityTint(tier.key).opacity(0.12), in: Capsule())
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(tier.open) open \(tier.label) \(tier.open == 1 ? "complaint" : "complaints")")
                }
            }
            .padding(.vertical, 1)
        }
        .scrollBounceBehavior(.basedOnSize)
    }

    private static func severityTint(_ key: String) -> Color {
        switch key {
        case "safety":      return .cavnarRed
        case "legal":       return .cavnarAmber
        case "operational": return .cavnarEmber
        default:            return .cavnarInk3
        }
    }

    // MARK: - The root cause

    /// The diagnosis, laid out as the argument it is: a cause, the
    /// alternative it is being chosen over, and what would settle it. The
    /// alternative and the confidence are not decoration — a single confident
    /// cause with nothing to weigh it against is exactly the shape of a
    /// plausible guess, and this card exists to not be that.
    private func diagnosisCard(_ d: ReviewDiagnosis, money: RevenueAtRisk?) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("Why this is happening")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.1)
                    .textCase(.uppercase)
                    .foregroundStyle(Color.cavnarEmber)
                Spacer(minLength: 0)
                Text(d.confidenceBand)
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(0.7)
                    .textCase(.uppercase)
                    .foregroundStyle(Self.confidenceTint(d.confidenceBand))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Self.confidenceTint(d.confidenceBand).opacity(0.14), in: Capsule())
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)

            Text("\(d.category.replacingOccurrences(of: "_", with: " ")) · \(d.mentionCount) negative reviews over \(d.windowDays) days"
                 + ((d.stale ?? false) ? " · older read" : ""))
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
                .padding(.horizontal, 16)
                .padding(.top, 4)
                .padding(.bottom, 12)

            Divider().overlay(Color.cavnarInk3.opacity(0.18))

            VStack(alignment: .leading, spacing: 13) {
                diagnosisRow("Most likely cause", d.cause)
                if let alt = d.alternativeCause {
                    diagnosisRow("It could also be", alt, quiet: true)
                }
                if let confirm = d.whatWouldConfirm {
                    diagnosisRow("What would tell them apart", confirm)
                }
                if let action = d.recommendedAction {
                    diagnosisRow("Do this", action)
                }
                if let outcome = d.expectedOutcome {
                    diagnosisRow("What should change", outcome, quiet: true)
                }
                if !d.operationalEvidence.isEmpty {
                    diagnosisRow(
                        "Cross-checked against",
                        d.operationalEvidence
                            .map { "\($0.module.replacingOccurrences(of: "_", with: " ")): \($0.metric) \($0.value)" }
                            .joined(separator: "  ·  "),
                        quiet: true)
                }
                if !d.evidenceReviewIds.isEmpty {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Reviews this rests on")
                            .font(.cavnarBody(10, weight: 700))
                            .tracking(0.9)
                            .textCase(.uppercase)
                            .foregroundStyle(Color.cavnarInk3)
                        Text(d.evidenceReviewIds.map { "#\($0)" }.joined(separator: ", "))
                            .font(.cavnarNumber(13))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Based on \(d.evidenceReviewIds.count) reviews")
                }
                if let m = money, m.available,
                   let low = m.monthlyLow, let high = m.monthlyHigh {
                    revenueBlock(low: low, high: high, m: m)
                }
            }
            .padding(16)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarInk3.opacity(0.05),
                    in: RoundedRectangle(cornerRadius: CavnarRadius.control))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .stroke(Color.cavnarInk3.opacity(0.18), lineWidth: 1)
        )
    }

    private func diagnosisRow(_ label: String, _ body: String, quiet: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(.cavnarBody(10, weight: 700))
                .tracking(0.9)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarInk3)
            Text(body)
                .font(.cavnarBody(quiet ? 13.5 : 14.5))
                .foregroundStyle(quiet ? Color.cavnarInk3 : Color.cavnarInk2)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label). \(body)")
    }

    /// Always labelled a forecast, always shown with what it was computed
    /// from. A point estimate here would be a fabricated precision; the range
    /// and the assumption travelling with it are the honest version.
    private func revenueBlock(low: Double, high: Double, m: RevenueAtRisk) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\("$" + abs(low).commaFormatted)–\("$" + abs(high).commaFormatted)")
                .font(.cavnarNumber(19, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text("a month \(m.direction == "at_risk" ? "at risk" : "of upside") from the "
                 + String(format: "%+.2f", m.ratingDelta ?? 0) + "★ move over 30 days")
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk2)
            if let assumption = m.assumption {
                Text("Forecast, not a measurement. \(assumption)")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color.cavnarEmber.opacity(0.09),
                    in: RoundedRectangle(cornerRadius: 9))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Forecast. \("$" + abs(low).commaFormatted) to \("$" + abs(high).commaFormatted) a month \(m.direction == "at_risk" ? "at risk" : "of upside").")
    }

    private static func confidenceTint(_ band: String) -> Color {
        switch band {
        case "high":   return .cavnarGreen
        case "medium": return .cavnarAmber
        default:       return .cavnarInk3
        }
    }

    private struct ParsedInsightLine {
        let symbol: String
        let tint: Color
        let text: String
        let isForecast: Bool
    }

    /// Maps the line's emoji prefix onto an SF Symbol. Unknown prefixes keep
    /// their text and fall back to a neutral bullet rather than being
    /// dropped — the passage is the point, the icon is decoration.
    private static func parseInsightLine(_ line: String) -> ParsedInsightLine {
        let table: [(String, String, Color, Bool)] = [
            ("📊", "chart.bar.fill", .cavnarBlue, false),
            ("⚠️", "exclamationmark.triangle.fill", .cavnarAmber, false),
            ("⚠", "exclamationmark.triangle.fill", .cavnarAmber, false),
            ("✅", "checkmark.circle.fill", .cavnarGreen, false),
            ("🚨", "exclamationmark.octagon.fill", .cavnarRed, false),
            ("💡", "lightbulb.fill", .cavnarAmber, false),
            // The "Why" line — the read's own root-cause sentence, drawn from
            // the stored diagnosis. Distinct symbol because it is a different
            // kind of claim from the measured line above it.
            ("🔍", "magnifyingglass", .cavnarEmber, false),
            ("🔮", "sparkles", .cavnarEmber, true),
        ]
        for (prefix, symbol, tint, forecast) in table where line.hasPrefix(prefix) {
            let body = line.dropFirst(prefix.count)
                .trimmingCharacters(in: CharacterSet(charactersIn: " \u{FE0F}"))
            return ParsedInsightLine(symbol: symbol, tint: tint, text: body, isForecast: forecast)
        }
        return ParsedInsightLine(symbol: "circle.fill", tint: .cavnarInk3, text: line, isForecast: false)
    }

    private var insightSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.45)
            CavnarSkeletonBar(height: 12, widthFraction: 0.95)
            CavnarSkeletonBar(height: 12, widthFraction: 0.85)
            CavnarSkeletonBar(height: 12, widthFraction: 0.7)
        }
        .padding(16)
        .background(Color.cavnarEmber.opacity(0.09))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    // MARK: - Loading skeletons

    private var performanceSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.5)
            HStack {
                ForEach(0..<3, id: \.self) { _ in
                    VStack(spacing: 6) {
                        CavnarSkeletonBar(height: 20, widthFraction: 0.5)
                        CavnarSkeletonBar(height: 10, widthFraction: 0.8)
                    }
                    .frame(maxWidth: .infinity)
                }
            }
        }
    }

    private var topicGridSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.35)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible())], spacing: 10) {
                ForEach(0..<4, id: \.self) { _ in
                    VStack(alignment: .leading, spacing: 8) {
                        CavnarSkeletonBar(height: 12, widthFraction: 0.6)
                        CavnarSkeletonBar(height: 20, widthFraction: 0.3)
                        CavnarSkeletonBar(height: 6, widthFraction: 1.0)
                    }
                    .padding(12)
                    .background(Color.cavnarPaper2.opacity(0.6))
                    .overlay(
                        RoundedRectangle(cornerRadius: CavnarRadius.control)
                            .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                }
            }
        }
    }

    private var trendChartSkeleton: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.45)
            CavnarSkeletonBar(height: 190, widthFraction: 1.0)
            HStack(spacing: 16) {
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
            }
        }
        // Unboxed, matching trendChartCard's own now-unboxed container —
        // otherwise the skeleton pops from boxed to unboxed the instant
        // real data arrives.
    }

    // MARK: - Topic sentiment grid

    private var topicGrid: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Topic sentiment")
                .font(.cavnarBody(14, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible())], spacing: 10) {
                ForEach(viewModel.heatmap.filter { $0.count > 0 }) { entry in
                    NavigationLink {
                        FilteredReviewsView(title: entry.label, category: entry.category)
                    } label: {
                        topicCard(entry)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func topicTone(_ entry: TopicHeatmapEntry) -> CavnarTone {
        if entry.pctNegative > 20 { return .bad }
        if entry.pctPositive >= 70 { return .good }
        return .warning
    }

    private func topicCard(_ entry: TopicHeatmapEntry) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 4) {
                Text(entry.label)
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                trendIcon(entry.trend)
                Spacer()
            }
            Text("\(entry.count)")
                .font(.cavnarNumber(20, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
            StatProgressBar(progress: Double(entry.pctPositive) / 100, tone: topicTone(entry))
            HStack {
                Text("\(entry.pctPositive)% pos")
                    .font(.cavnarNumber(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                Spacer()
                Text("\(entry.pctNegative)% neg")
                    .font(.cavnarNumber(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarRed)
            }
        }
        .padding(12)
        .background(Color.cavnarPaper2.opacity(0.6))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    @ViewBuilder
    private func trendIcon(_ trend: String) -> some View {
        switch trend {
        case "up": Image(systemName: "arrow.up.right").font(.system(size: 9)).foregroundStyle(Color.cavnarRed)
        case "down": Image(systemName: "arrow.down.right").font(.system(size: 9)).foregroundStyle(Color.cavnarGreen)
        default: EmptyView()
        }
    }

}
