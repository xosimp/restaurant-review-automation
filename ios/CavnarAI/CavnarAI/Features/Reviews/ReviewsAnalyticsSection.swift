import SwiftUI

struct ReviewsAnalyticsSection: View {
    let viewModel: ReviewsAnalyticsViewModel

    @State private var selectedTopic: TopicWeekRow?
    /// A review a diagnosis cites, tapped open.
    @State private var evidenceTarget: EvidenceTarget?

    struct EvidenceTarget: Hashable {
        let reviewID: Int
        let category: String
    }

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
                // A reason the read gave that no stored diagnosis backs (H2).
                if viewModel.causesUnverified {
                    CavnarCaveat.unverifiedCauses(viewModel.unsupportedCauses)
                }
                if viewModel.insightIsStale {
                    CavnarCaveat.olderRead(asOf: viewModel.insightAsOf)
                }

                // The AI read itself. It was fetched on every open and then
                // never rendered — so the app paid for a Haiku call, threw
                // the answer away, and could still show the caveat above
                // referring to a passage that was not on screen.
                if let insight = viewModel.insight, !insight.isEmpty {
                    // The ember thread: the chart above to the read below.
                    EmberThread().padding(.leading, 6)
                    insightCard(insight)
                    // Next week's rating, computed from the fitted trend in
                    // Python (H8) — tagged so it never reads as the model's.
                    if let forecast = viewModel.ratingForecast?.line {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            HomeMixedText.make(forecast, size: 13, weight: 500, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                            ClaimKindTag(kind: "forecast")
                        }
                    }
                    // Open complaints ranked by how serious they are rather
                    // than how many there are. Sits directly under the read
                    // because it is the same question that read is answering.
                    // The unclassified count shows even with no tier open.
                    if !viewModel.severityTiers.isEmpty || viewModel.unclassifiedCount > 0 {
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
                    diagnosisCard(diagnosis)
                }
                // The whole restaurant's revenue range, from the move in its
                // all-time rating — its own block, never inside a complaint's
                // card, where it read as what that complaint costs (M-19).
                if let m = viewModel.revenueAtRisk, m.available,
                   let low = m.monthlyLow, let high = m.monthlyHigh {
                    revenueBlock(low: low, high: high, m: m)
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
        .navigationDestination(item: $evidenceTarget) { target in
            ReviewByIdView(reviewID: target.reviewID, category: target.category)
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
            // Which way the rating is moving, and how sure that direction is
            // — decoded all along and never shown.
            ratingTrendLine
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                let parsed = Self.parseInsightLine(line)
                let claim = Self.claimKey(forInsightLine: line).flatMap { viewModel.claimKinds[$0] }
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
                    VStack(alignment: .leading, spacing: 6) {
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
                        // Measured / inferred / suggestion — what kind of
                        // statement this line is (claim_kinds).
                        if claim != nil {
                            ClaimKindTag(kind: claim)
                                .padding(.leading, 22)
                        }
                        // The answer row sits under the line it answers —
                        // the "Do today" line, matched by the text the
                        // server keyed.
                        if let rec = Self.rec(for: line, in: viewModel.insightRecs) {
                            // The line's own confidence (E13), not the
                            // rating trend's.
                            if let c = rec.confidenceDetail {
                                ConfidenceLine(confidence: c, recKey: rec.key, surface: "reviews", module: "reviews")
                                    .padding(.leading, 22)
                            }
                            RecAnswerRow(key: rec.key, surface: "reviews")
                                .padding(.leading, 22)
                        }
                    }
                }
            }
            // A keyed line the passage no longer carries verbatim (the
            // model's line was reworded on the way through) keeps its
            // controls rather than losing them.
            ForEach(Self.unplacedRecs(viewModel.insightRecs, lines: lines)) { rec in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(Color.cavnarGreen)
                            .accessibilityHidden(true)
                        Text(rec.text)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk2)
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let c = rec.confidenceDetail {
                        ConfidenceLine(confidence: c, recKey: rec.key, surface: "reviews", module: "reviews")
                            .padding(.leading, 22)
                    }
                    RecAnswerRow(key: rec.key, surface: "reviews")
                        .padding(.leading, 22)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.ai)
    }

    /// The keyed recommendation this passage line carries, if any.
    static func rec(for line: String, in recs: [ReviewInsightRec]) -> ReviewInsightRec? {
        let trimmed = { (s: String) in s.trimmingCharacters(in: .whitespacesAndNewlines) }
        return recs.first { rec in
            let text = trimmed(rec.text)
            return !text.isEmpty && line.contains(text)
        }
    }

    /// Keyed recommendations no passage line carries.
    static func unplacedRecs(_ recs: [ReviewInsightRec], lines: [String]) -> [ReviewInsightRec] {
        recs.filter { r in !lines.contains { line in Self.rec(for: line, in: [r]) != nil } }
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
                // The ones no tier was given, said rather than left out.
                if viewModel.unclassifiedCount > 0 {
                    HStack(spacing: 6) {
                        Text("\(viewModel.unclassifiedCount)")
                            .font(.cavnarNumber(12.5, weight: 700))
                        Text("unclassified")
                            .font(.cavnarBody(11.5))
                    }
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Color.cavnarPaper3.opacity(0.6), in: Capsule())
                    .accessibilityElement(children: .combine)
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
        // Ember is emphasis, never a severity (B4 L7).
        case "operational": return .cavnarInk2
        default:            return .cavnarInk3
        }
    }

    // MARK: - The root cause

    /// The diagnosis, laid out as the argument it is: a cause, the
    /// alternative it is being chosen over, and what would settle it. The
    /// alternative and the confidence are not decoration — a single confident
    /// cause with nothing to weigh it against is exactly the shape of a
    /// plausible guess, and this card exists to not be that.
    private func diagnosisCard(_ d: ReviewDiagnosis) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(OwnerCopy.diagnosisHeading)
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.1)
                    .textCase(.uppercase)
                    .foregroundStyle(Color.cavnarEmber)
                Spacer(minLength: 0)
                // The cause is the model's read of the reviews — said so.
                ClaimKindTag(kind: viewModel.claimKinds["why"])
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)

            HomeMixedText.make("\(d.category.replacingOccurrences(of: "_", with: " ")) · \(d.mentionCount) negative reviews over \(d.windowDays) days"
                 + (d.asOf.map { " · read \($0)" } ?? "")
                 + ((d.stale ?? false) && d.staleNote == nil ? " · older read" : ""),
                               size: 12, weight: 400, color: .cavnarInk3)
                .padding(.horizontal, 16)
                .padding(.top, 4)
                .padding(.bottom, d.trust == nil ? 12 : 8)

            // How sure, as a percentage with what it rests on (K1/K6) — the
            // shared confidence line, not a bare band capsule.
            if let c = d.trust {
                ConfidenceLine(confidence: c, recKey: d.recKey, surface: "reviews", module: "reviews")
                    .padding(.horizontal, 16)
                    .padding(.bottom, 12)
            }

            // The server's own sentence for a read that hasn't been
            // refreshed — the same caveat the insight above uses.
            if let note = d.staleNote, !note.isEmpty {
                CavnarCaveat(title: "Older read", detail: note)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 12)
            }
            // A figure in the cause that could not be traced to the data,
            // stored with the read so the card says so here too (M-17).
            if let figs = d.unsupportedFigures, !figs.isEmpty {
                CavnarCaveat.unverifiedFigures(figs)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 12)
            }

            Divider().overlay(Color.cavnarInk3.opacity(0.18))

            VStack(alignment: .leading, spacing: 13) {
                diagnosisRow("Most likely cause", d.cause)
                if let alt = d.alternativeCause {
                    diagnosisRow("It could also be", alt, quiet: true)
                }
                if let confirm = d.whatWouldConfirm {
                    diagnosisRow("What would tell them apart", confirm)
                }
                // An action the owner already answered stays answered: the
                // card keeps its evidence and drops the action.
                if let action = d.recommendedAction, d.answered != true {
                    VStack(alignment: .leading, spacing: 6) {
                        diagnosisRow("Do this", action)
                        if let key = d.recKey {
                            RecAnswerRow(key: key, surface: "reviews")
                        }
                    }
                }
                // Conditional on the cause, never a promise (NS1 H10).
                if let outcome = OwnerCopy.expectedOutcome(d.expectedOutcome) {
                    diagnosisRow(OwnerCopy.expectedOutcomeLabel, outcome, quiet: true)
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
                    VStack(alignment: .leading, spacing: 7) {
                        Text("Reviews this rests on")
                            .font(.cavnarBody(10, weight: 700))
                            .tracking(0.9)
                            .textCase(.uppercase)
                            .foregroundStyle(Color.cavnarInk3)
                            .accessibilityLabel("Based on \(d.evidenceReviewIds.count) reviews")
                        // Each cited review opens — the web's jumpToReview.
                        AccountFlowLayout(spacing: 6, lineSpacing: 6) {
                            ForEach(d.evidenceReviewIds, id: \.self) { id in
                                evidenceChip(id, category: d.category, recKey: d.recKey)
                            }
                        }
                    }
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

    /// "Review #412" as a tappable chip — AccountChip's muted look, with the
    /// id in the number face. Opens that review (ReviewByIdView).
    private func evidenceChip(_ id: Int, category: String, recKey: String? = nil) -> some View {
        Button {
            Haptic.light()
            evidenceTarget = EvidenceTarget(reviewID: id, category: category)
            // Opening a review the diagnosis rests on is evidence viewed (#38).
            RecEvidenceLog.viewed(key: recKey, surface: "reviews", module: "reviews")
        } label: {
            (Text("Review ") + Text("#\(id)").font(.cavnarNumber(13, weight: 600)))
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(Color.cavnarInk2)
                .padding(.horizontal, 11)
                .padding(.vertical, 6)
                .background(Color.white.opacity(0.04))
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.08), lineWidth: 1))
                .clipShape(Capsule())
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Open review \(id)")
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
            Text("a month \(m.direction == "at_risk" ? "at risk" : "of upside") across the restaurant, from the "
                 + String(format: "%+.2f", m.ratingDelta ?? 0) + "★ move in your all-time rating over 30 days")
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

    /// "Trend strength 62%" — the rating trend's measured strength
    /// (trend_strength_pct), never a band word: "medium confidence" read
    /// the same for a zigzag as for a steady slide (B4 L2, B1 H8). Nil when
    /// the server sent no figure.
    static func trendStrengthLabel(_ pct: Int?) -> String? {
        guard let pct else { return nil }
        return "Trend strength \(max(0, min(100, pct)))%"
    }

    /// The claim_kinds key an insight line is: 📊 this_week, ⚠️ watch,
    /// ✅ do_today, 🔮 next_week.
    static func claimKey(forInsightLine line: String) -> String? {
        let t = line.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("📊") { return "this_week" }
        if t.hasPrefix("⚠") { return "watch" }
        if t.hasPrefix("✅") { return "do_today" }
        if t.hasPrefix("🔮") { return "next_week" }
        return nil
    }

    @ViewBuilder
    private var ratingTrendLine: some View {
        if let sentence = viewModel.ratingTrend?.sentence {
            let strength = viewModel.ratingTrend?.trendStrengthPct
            let label = Self.trendStrengthLabel(strength)
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                (HomeMixedText.make(sentence, size: 12.5, weight: 600, color: .cavnarInk2)
                 + (label.map {
                     HomeMixedText.make(" \u{00B7} " + $0, size: 12.5, weight: 700,
                                        color: ConfidenceDisplay.tone(pct: strength).color)
                 } ?? Text(verbatim: "")))
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                ClaimKindTag(kind: viewModel.claimKinds["rating_trend"])
            }
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
