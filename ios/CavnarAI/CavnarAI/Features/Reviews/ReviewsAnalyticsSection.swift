import SwiftUI

/// Reviews → Analytics on the phone (readability round 10/8/26): Cavnar
/// AI's read, the money the rating's move implies, the most likely cause as
/// an answer card, the three topics guests talk about most, and how you
/// compare. The weekly topic grid, the sentiment river and the approval
/// rings are the web's ("Full analysis" opens it) — "Web explains. iPhone
/// decides." The chart views stay in the project for now (candidate for
/// future cleanup after additional verification).
struct ReviewsAnalyticsSection: View {
    let viewModel: ReviewsAnalyticsViewModel

    /// A review a diagnosis cites, tapped open.
    @State private var evidenceTarget: EvidenceTarget?
    /// The caveat banners behind the one-line summary (density #34).
    @State private var showingCaveats = false

    /// One line for every caveat on the read — nil when there is none.
    private var caveatSummary: String? {
        Self.caveatSummary(unverified: !viewModel.unsupportedFigures.isEmpty || !viewModel.unsupportedNames.isEmpty
                                        || viewModel.causesUnverified,
                           stale: viewModel.insightIsStale)
    }

    static func caveatSummary(unverified: Bool, stale: Bool) -> String? {
        switch (unverified, stale) {
        case (true, true): return "Some of this read is unverified, and it is an older read."
        case (true, false): return "Some of this read couldn\u{2019}t be checked against your data."
        case (false, true): return "This is an older read."
        default: return nil
        }
    }

    struct EvidenceTarget: Hashable {
        let reviewID: Int
        let category: String
    }

    // Each section shows its own skeleton while it's individually still in
    // flight rather than gating the whole page behind one spinner — the
    // requests in ReviewsAnalyticsViewModel.load() run concurrently but are
    // awaited (and so become non-nil) in a fixed order, so e.g. the AI
    // insight — the slowest, since it's an LLM call — used to visibly "pop
    // in" a couple seconds after everything else had already rendered.
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: CavnarSpace.l) {
                // The period picker lives in the topics card, the one thing
                // it moves (re-audit 10/8/26 M12) — at the top it read as if
                // the read, the cause and the money moved with it.
                // The restaurant's own read first, its money figure next,
                // the cause, the topics, and How you compare LAST (density
                // #34) — a peer's number before your own read the wrong way
                // round. The caveat banners are one line, behind "Why?".
                if let line = caveatSummary {
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
                        // Raised when the backend could not tie every figure
                        // in the AI passage back to this restaurant's own
                        // data — see ai_guard.verify_figures.
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
                    }
                }

                // A read the server could not write, in its own words
                // (InsightRefresh.follow, re-audit 10/8/26 #3).
                if let message = viewModel.insightError {
                    CavnarCaveat.readUnavailable(message)
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
                } else if viewModel.isLoading || viewModel.insightPending {
                    insightSkeleton
                }

                // The whole restaurant's revenue range, from the move in its
                // all-time rating — its own block, never inside a complaint's
                // card, where it read as what that complaint costs (M-19).
                // The tab's money figure, right under the read (density #34).
                if let m = viewModel.revenueAtRisk, m.available,
                   let low = m.monthlyLow, let high = m.monthlyHigh {
                    revenueBlock(low: low, high: high, m: m)
                }
                // The root-cause card. It renders only when a diagnosis
                // actually exists: an owner whose reviews do not yet support
                // a cause sees nothing here, never a cause produced to fill
                // the space.
                if let diagnosis = viewModel.diagnosis {
                    diagnosisCard(diagnosis)
                }

                // What guests talk about, three rows; the weekly grid, the
                // sentiment river and the approvals are the web's (#60).
                topTopics

                // How you compare — the Benchmark Engine's card (#23) —
                // after the restaurant's own read (density #34).
                HowYouCompareCard(module: "reviews")
            }
            .padding(CavnarSpace.gutter)
        }
        .navigationDestination(item: $evidenceTarget) { target in
            ReviewByIdView(reviewID: target.reviewID, category: target.category)
        }
    }

    // MARK: - The AI read

    /// The endpoint writes 3-4 prefixed lines (📊 this week / ⚠️ watch /
    /// ✅ do today / 🔮 next week, and 🔍 why from a stored diagnosis).
    /// One answer card (re-audit 10/8/26 M10/M11): the rating's move (or the
    /// week's line) as the headline, one sentence under it, the cause, and
    /// the one action with its answer row — then the rest of the read behind
    /// "See the evidence". When the diagnosis card renders, it carries the
    /// cause and the action: the read's 🔍 line and its own action line drop,
    /// so the cause and the action are each said once.
    private func insightCard(_ insight: String) -> some View {
        let lines = insight
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let read = Self.answerParts(lines: lines, trendSentence: viewModel.ratingTrend?.sentence,
                                    hasDiagnosis: viewModel.diagnosis != nil)
        let actionRec = read.action.flatMap { Self.rec(for: $0, in: viewModel.insightRecs) }
        let placed = Self.actionRecs(viewModel.insightRecs, lines: lines)
        let hasForecastLine = lines.contains { Self.parseInsightLine($0).isForecast }
        // Keyed recs no line carries keep their controls — unless the
        // diagnosis card is the one action on screen.
        let unplaced = viewModel.diagnosis == nil
            ? viewModel.insightRecs.filter { r in !placed.contains { $0.key == r.key } } : []
        return CavnarAnswerCard(
            kicker: "Cavnar AI\u{2019}s read on your reviews",
            headline: read.headline,
            summary: read.summary,
            cause: read.why,
            isHypothesis: viewModel.causesUnverified,
            detailLabel: "See the evidence"
        ) {
            if let action = read.action {
                let text = Self.parseInsightLine(action).text
                (Text("Do this: ").font(.cavnarBody(CavnarType.body, weight: 700)).foregroundStyle(Color.cavnarInk)
                    + HomeMixedText.make(text, role: .body))
                    .fixedSize(horizontal: false, vertical: true)
                if let rec = actionRec {
                    recControls(rec, indent: 0)
                }
            }
        } detail: {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                // How steady the rating's move is — its measured strength.
                ratingTrendStrength
                ForEach(Array(read.rest.enumerated()), id: \.offset) { _, line in
                    let parsed = Self.parseInsightLine(line)
                    let claim = Self.claimKey(forInsightLine: line).flatMap { viewModel.claimKinds[$0] }
                    if parsed.isForecast {
                        forecastRow(parsed.text)
                    } else {
                        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Image(systemName: parsed.symbol)
                                    .font(.cavnar(.caption))
                                    .foregroundStyle(parsed.tint)
                                    .accessibilityHidden(true)
                                CavnarMixedText(parsed.text, role: .body)
                            }
                            .accessibilityElement(children: .combine)
                            .accessibilityLabel(parsed.text)
                            // Inferred / forecast — only where the line is
                            // not simply measured.
                            if Self.tagsClaim(claim) {
                                ClaimKindTag(kind: claim)
                                    .padding(.leading, 22)
                            }
                        }
                    }
                }
                // The computed forecast (H8), when the passage carries no
                // forecast of its own — one forecast on the card, never two.
                if !hasForecastLine, let forecast = viewModel.ratingForecast?.line {
                    forecastRow(forecast)
                }
                // A keyed line the passage no longer carries verbatim keeps
                // its controls.
                ForEach(unplaced) { rec in
                    VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarGreen)
                                .accessibilityHidden(true)
                            CavnarMixedText(rec.text, role: .body)
                        }
                        recControls(rec)
                    }
                }
            }
        }
    }

    /// The read split for the answer card: the headline (the rating's move,
    /// else the week's line), one sentence, the cause (the 🔍 line — only
    /// with no diagnosis card), the action line (only with no diagnosis
    /// card), and everything else for "See the evidence".
    struct AnswerParts: Equatable {
        var headline: String
        var summary: String?
        var why: String?
        var action: String?
        var rest: [String]
    }

    static func answerParts(lines: [String], trendSentence: String?, hasDiagnosis: Bool) -> AnswerParts {
        var rest = lines
        func take(_ key: String) -> String? {
            guard let i = rest.firstIndex(where: { claimKey(forInsightLine: $0) == key }) else { return nil }
            return rest.remove(at: i)
        }
        let whyIndex = rest.firstIndex { $0.hasPrefix("🔍") }
        let why = whyIndex.map { rest.remove(at: $0) }
        let action = take("do_today")
        let thisWeek = take("this_week")
        let headline: String
        var summary: String?
        if let trend = trendSentence?.trimmingCharacters(in: .whitespaces), !trend.isEmpty {
            headline = trend
            summary = thisWeek.map { parseInsightLine($0).text } ?? take("watch").map { parseInsightLine($0).text }
        } else if let thisWeek {
            headline = parseInsightLine(thisWeek).text
            summary = take("watch").map { parseInsightLine($0).text }
        } else if !rest.isEmpty {
            headline = parseInsightLine(rest.removeFirst()).text
        } else {
            headline = action.map { parseInsightLine($0).text } ?? ""
        }
        return AnswerParts(headline: headline, summary: summary,
                           why: hasDiagnosis ? nil : why.map { parseInsightLine($0).text },
                           action: hasDiagnosis ? nil : action,
                           rest: rest)
    }

    /// The action line's own confidence (E13), not the rating trend's, and
    /// Done / Not for us.
    @ViewBuilder
    private func recControls(_ rec: ReviewInsightRec, indent: CGFloat = 22) -> some View {
        if let c = rec.confidenceDetail {
            ConfidenceLine(confidence: c, recKey: rec.key, surface: "reviews", module: "reviews")
                .padding(.leading, indent)
        }
        RecAnswerRow(key: rec.key, surface: "reviews")
            .padding(.leading, indent)
    }

    /// A forecast: Secondary, tagged so it never reads as a measurement.
    private func forecastRow(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            ClaimKindTag(kind: "forecast")
            CavnarMixedText(text, role: .secondary)
        }
        .padding(.leading, 10)
        .padding(.vertical, CavnarSpace.xxs)
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.cavnarEmber.opacity(0.7)).frame(width: 2)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Forecast. \(text)")
    }

    /// A claim tag is worth its space only where the line is not simply
    /// measured: inferred, a forecast, an estimate.
    static func tagsClaim(_ kind: String?) -> Bool {
        guard let k = kind?.lowercased() else { return false }
        return k == "inferred" || k == "forecast" || k == "estimate"
    }

    /// The "Do today" line — the one the answer row belongs under.
    static func isActionLine(_ line: String) -> Bool {
        claimKey(forInsightLine: line) == "do_today"
    }

    /// The keyed recommendations the passage's action line carries — the
    /// ones whose controls render in place; every other keyed rec renders
    /// in its own row so no controls are lost.
    static func actionRecs(_ recs: [ReviewInsightRec], lines: [String]) -> [ReviewInsightRec] {
        recs.filter { r in lines.contains { isActionLine($0) && rec(for: $0, in: [r]) != nil } }
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
    /// Only the tiers with something open; the unclassified count is the
    /// web's (readability round 10/8/26).
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
                            .font(.cavnarNumber(CavnarType.caption, weight: 700))
                        Text(tier.label)
                            .font(.cavnar(.caption))
                    }
                    .foregroundStyle(Self.severityTint(tier.key))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
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
        case "safety":      return .cavnarRedText
        case "legal":       return .cavnarAmber
        // Ember is emphasis, never a severity (B4 L7).
        case "operational": return .cavnarInk2
        default:            return .cavnarInk2
        }
    }

    // MARK: - The root cause

    /// The diagnosis as an answer card (readability round 10/8/26): the
    /// cause is the headline, what it rests on is the one line under it,
    /// then the confidence, the action with its answer row, the conditional
    /// outcome and ONE visible alternative. What would tell the two apart,
    /// the cross-checks and the reviews it rests on are behind "Show the
    /// reasoning". The alternative and the confidence are not decoration —
    /// a single confident cause with nothing to weigh it against is exactly
    /// the shape of a plausible guess, and this card exists to not be that.
    private func diagnosisCard(_ d: ReviewDiagnosis) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                CavnarKicker(OwnerCopy.diagnosisHeading)
                Spacer(minLength: 0)
                // The cause is the model's read of the reviews — said so.
                ClaimKindTag(kind: viewModel.claimKinds["why"])
            }
            // The server's own sentence for a read that hasn't been
            // refreshed — the same caveat the insight above uses.
            if let note = d.staleNote, !note.isEmpty {
                CavnarCaveat(title: "Older read", detail: note)
            } else if d.isOlderRead {
                // Too old to lean on (memory round): kept for its evidence,
                // never answered as a live recommendation.
                CavnarCaveat(title: "Older read",
                             detail: "Past its refresh \u{2014} kept for the reviews it rests on, not as a live recommendation.")
            }
            // A figure in the cause that could not be traced to the data,
            // stored with the read so the card says so here too (M-17).
            if let figs = d.unsupportedFigures, !figs.isEmpty {
                CavnarCaveat.unverifiedFigures(figs)
            }
            CavnarAnswerCard(
                headline: d.cause,
                summary: Self.diagnosisBasis(d),
                alternativeCause: d.alternativeCause,
                // Conditional on the cause, never a promise (NS1 H10).
                expectedOutcome: OwnerCopy.expectedOutcome(d.expectedOutcome).map { "If this is the cause: \($0)" },
                // How sure, as a percentage with what it rests on (K1/K6).
                confidence: d.trust.map {
                    ConfidenceLine(confidence: $0, recKey: d.recKey, surface: "reviews", module: "reviews")
                },
                detailLabel: "See the evidence",
                surface: nil
            ) {
                // An action the owner already answered stays answered: the
                // card keeps its evidence and drops the action.
                if let action = d.recommendedAction, d.answered != true {
                    // An older read's action is what it suggested then, with
                    // no answer controls (controls_withheld).
                    (Text(d.isOlderRead ? "It suggested then: " : "Do this: ")
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                        .foregroundStyle(d.isOlderRead ? Color.cavnarInk2 : Color.cavnarInk)
                     + HomeMixedText.make(action, role: .body, color: d.isOlderRead ? .cavnarInk2 : .cavnarInk))
                        .fixedSize(horizontal: false, vertical: true)
                    if d.showsControls, let key = d.recKey {
                        RecAnswerRow(key: key, surface: "reviews")
                    }
                }
            } detail: {
                diagnosisReasoning(d)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.ai)
    }

    /// "Cold food · 6 negative reviews · 30 days · read 10/7/26".
    static func diagnosisBasis(_ d: ReviewDiagnosis) -> String {
        let topic = d.category.replacingOccurrences(of: "_", with: " ")
        var s = "\(topic.prefix(1).uppercased() + topic.dropFirst()) \u{00B7} \(d.mentionCount) negative "
            + "\(d.mentionCount == 1 ? "review" : "reviews") \u{00B7} \(d.windowDays) days"
        if let asOf = d.asOf, !asOf.isEmpty { s += " \u{00B7} read \(asOf)" }
        if ((d.stale ?? false) || d.isOlderRead) && d.staleNote == nil { s += " \u{00B7} older read" }
        return s
    }

    /// What would tell the cause from its alternative, what it was checked
    /// against, and the reviews it rests on — each one opens.
    @ViewBuilder
    private func diagnosisReasoning(_ d: ReviewDiagnosis) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            if let confirm = d.whatWouldConfirm, !confirm.isEmpty {
                reasoningRow("What would tell them apart", confirm)
            }
            if !d.operationalEvidence.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("Checked against")
                    ForEach(Array(d.operationalEvidence.enumerated()), id: \.offset) { _, e in
                        CavnarMixedText(Self.crossCheckLine(e), role: .secondary)
                    }
                }
            }
            if !d.evidenceReviewIds.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    CavnarKicker("Reviews this rests on")
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
    }

    /// One cross-check in an owner's words: "Labor · overtime hours: 12"
    /// rather than "labor: overtime_hours 12".
    static func crossCheckLine(_ e: ReviewDiagnosis.OperationalEvidence) -> String {
        let module: String
        switch e.module.lowercased() {
        case "labor": module = "Labor"
        case "food_cost", "inventory": module = "Food cost"
        case "pos", "sales": module = "Sales"
        case "reviews": module = "Reviews"
        case "marketing": module = "Marketing"
        default:
            let m = e.module.replacingOccurrences(of: "_", with: " ")
            module = m.prefix(1).uppercased() + m.dropFirst()
        }
        let (metric, unit) = metricWords(e.metric)
        return "\(module) \u{00B7} \(metric): \(e.value)\(unit)"
    }

    /// A metric key in an owner's words, and its unit: "avg_ticket_time_min"
    /// → ("Average ticket time", " min"), "labor_pct" → ("Labor", "%")
    /// (re-audit 10/8/26 L15 — the raw key read "avg ticket time min: 12").
    static func metricWords(_ raw: String) -> (String, String) {
        var words = raw.replacingOccurrences(of: "_", with: " ")
            .lowercased()
            .split(separator: " ")
            .map(String.init)
        var unit = ""
        if words.count > 1, let last = words.last {
            switch last {
            case "min", "mins", "minutes": unit = " min"; words.removeLast()
            case "pct", "percent", "%": unit = "%"; words.removeLast()
            case "hrs", "hr", "hours": unit = " h"; words.removeLast()
            case "usd", "dollars": unit = ""; words.removeLast()
            default: break
            }
        }
        let expand = ["avg": "average", "qty": "quantity", "ot": "overtime", "num": "number of",
                      "cnt": "count", "pos": "sales", "ttl": "total", "foh": "front of house",
                      "boh": "back of house", "pm": "PM", "am": "AM"]
        let sentence = words.map { expand[$0] ?? $0 }.joined(separator: " ")
        guard let first = sentence.first else { return (raw, unit) }
        return (first.uppercased() + sentence.dropFirst(), unit)
    }

    /// A cited review as its guest's first name and stars ("Maria ★★") —
    /// read when the reasoning opens, never "Review #412". Opens that review
    /// (ReviewByIdView).
    private func evidenceChip(_ id: Int, category: String, recKey: String? = nil) -> some View {
        Button {
            Haptic.light()
            evidenceTarget = EvidenceTarget(reviewID: id, category: category)
            // Opening a review the diagnosis rests on is evidence viewed (#38).
            RecEvidenceLog.viewed(key: recKey, surface: "reviews", module: "reviews")
        } label: {
            DiagnosisEvidenceLabel(reviewID: id)
                .padding(.horizontal, 12)
                .frame(minHeight: 44)
                .background(Color.white.opacity(0.04))
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.1), lineWidth: 1))
                .clipShape(Capsule())
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    private func reasoningRow(_ label: String, _ body: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker(label)
            CavnarMixedText(body, role: .secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label). \(body)")
    }

    /// Always labelled a forecast, always shown with what it was computed
    /// from. A point estimate here would be a fabricated precision; the range
    /// and the assumption travelling with it are the honest version.
    private func revenueBlock(low: Double, high: Double, m: RevenueAtRisk) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            Text("\("$" + abs(low).commaFormatted)–\("$" + abs(high).commaFormatted)")
                .cavnarText(.figureM)
            CavnarMixedText("a month \(m.direction == "at_risk" ? "at risk" : "of upside") across the restaurant, from the "
                            + String(format: "%+.2f", m.ratingDelta ?? 0) + "\u{2605} move in your all-time rating over 30 days",
                            role: .secondary)
            if let assumption = m.assumption {
                Text("Forecast, not a measurement. \(assumption)")
                    .cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(CavnarSpace.s)
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
        if t.unicodeScalars.first == "\u{26A0}" { return "watch" }   // "⚠" or "⚠️" (a Character compare misses the variation selector)
        if t.hasPrefix("✅") { return "do_today" }
        if t.hasPrefix("🔮") { return "next_week" }
        return nil
    }

    /// The rating trend's measured strength as a confidence line (a meter
    /// and the real %) — the trend's sentence is the read's headline.
    @ViewBuilder
    private var ratingTrendStrength: some View {
        if viewModel.ratingTrend?.sentence != nil {
            let strength = viewModel.ratingTrend?.trendStrengthPct
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                if Self.tagsClaim(viewModel.claimKinds["rating_trend"]) {
                    ClaimKindTag(kind: viewModel.claimKinds["rating_trend"])
                }
                if let label = Self.trendStrengthLabel(strength), let pct = strength {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        ConfidenceMeter(fraction: Double(max(0, min(100, pct))) / 100,
                                        tone: ConfidenceDisplay.tone(pct: strength))
                            .alignmentGuide(.firstTextBaseline) { dim in dim[.bottom] + 1 }
                        HomeMixedText.make(label, role: .caption,
                                           color: .cavnarInk2,
                                           numberColor: ConfidenceDisplay.tone(pct: strength).color)
                    }
                    .accessibilityElement(children: .combine)
                }
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

    // MARK: - Top topics

    /// The three topics guests mention most in the window — the label, the
    /// count and which way it is moving — each opening its reviews, then
    /// the web's full analysis (the weekly grid, the sentiment river and
    /// the approvals) one tap away (readability round 10/8/26 #60).
    @ViewBuilder
    private var topTopics: some View {
        let top = Array(viewModel.heatmap.filter { $0.count > 0 }.sorted { $0.count > $1.count }.prefix(3))
        // A period with nothing in it keeps the card (and its period
        // control), so the owner can switch back.
        if !top.isEmpty || viewModel.windowDays != ReviewsAnalyticsViewModel.defaultWindowDays {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("What guests talk about")
                topicPeriod
                if top.isEmpty {
                    Text(viewModel.isLoadingTopics ? " " : "No topics mentioned in this period.")
                        .cavnarText(.secondary)
                        .frame(minHeight: 44, alignment: .leading)
                }
                VStack(spacing: 0) {
                    ForEach(Array(top.enumerated()), id: \.element.id) { index, entry in
                        NavigationLink {
                            FilteredReviewsView(title: entry.label, category: entry.category)
                        } label: {
                            topicRow(entry)
                        }
                        .buttonStyle(.plain)
                        .overlay(alignment: .top) {
                            if index > 0 { Rectangle().fill(Color.cavnarPaper3).frame(height: 1) }
                        }
                    }
                }
                CavnarWebLinkRow(title: "Full analysis",
                                 subtitle: "Topics week by week, sentiment and how replies were approved",
                                 path: "reviews/analytics", actionLabel: "Open on the web")
            }
            .cavnarCard()
        } else if viewModel.isLoading {
            topicSkeleton
        } else {
            CavnarWebLinkRow(title: "Full analysis", path: "reviews/analytics", actionLabel: "Open on the web")
        }
    }

    /// The period the topic rows cover — the one thing on this tab it moves
    /// (re-audit 10/8/26 M12), in the house segmented control.
    private var topicPeriod: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            Text("Mentions over the last")
                .cavnarText(.caption, color: .cavnarInk2)
            CavnarSegmentedControl(selection: Binding(
                get: { viewModel.windowDays },
                set: { days in
                    guard days != viewModel.windowDays else { return }
                    Haptic.light()
                    Task { await viewModel.setWindow(days) }
                }), options: [30, 90, 180], accessibilityTitle: "Time range") { days in
                    days == 180 ? "6 months" : "\(days) days"
                }
        }
        .padding(.bottom, CavnarSpace.xxs)
    }

    private func topicRow(_ entry: TopicHeatmapEntry) -> some View {
        HStack(alignment: .center, spacing: CavnarSpace.s) {
            Text(entry.label)
                .cavnarText(.label)
                .lineLimit(1)
            Spacer(minLength: CavnarSpace.xs)
            Text("\(entry.count)")
                .cavnarText(.figureS)
            trendIcon(entry.trend)
                .frame(width: 18)
            Image(systemName: "chevron.right")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .accessibilityHidden(true)
        }
        .frame(minHeight: 44)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(entry.label), \(entry.count) mentions\(Self.trendWords(entry.trend))")
    }

    private static func trendWords(_ trend: String) -> String {
        switch trend {
        case "up": return ", complaints rising"
        case "down": return ", complaints easing"
        default: return ""
        }
    }

    /// The topic's complaint trend: up is worse (red), down is better.
    @ViewBuilder
    private func trendIcon(_ trend: String) -> some View {
        switch trend {
        case "up": Image(systemName: "arrow.up.right").font(.cavnar(.caption)).foregroundStyle(Color.cavnarRed)
        case "down": Image(systemName: "arrow.down.right").font(.cavnar(.caption)).foregroundStyle(Color.cavnarGreen)
        default: Image(systemName: "minus").font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
        }
    }

    private var topicSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.35)
            ForEach(0..<3, id: \.self) { _ in
                CavnarSkeletonBar(height: 14, widthFraction: 0.9)
            }
        }
    }
}

/// A cited review's guest and stars ("Maria ★★"), read once per review per
/// launch (GET /mobile/api/reviews/<id>, the route ReviewByIdView reads);
/// "A review" until it lands or when it can't be read — never its id.
private struct DiagnosisEvidenceLabel: View {
    let reviewID: Int
    @State private var who: (name: String, stars: Int?)?

    @MainActor private static var cache: [Int: (name: String, stars: Int?)] = [:]

    private struct OneResponse: Decodable {
        let ok: Bool
        let review: Review?
    }

    var body: some View {
        HStack(spacing: CavnarSpace.xxs + 2) {
            Text(who?.name ?? "A review")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarInk2)
            if let stars = who?.stars, stars > 0 {
                Text(String(repeating: "\u{2605}", count: min(stars, 5)))
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarAmber)
                    .accessibilityHidden(true)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(who.map { "Open \($0.name)\u{2019}s review" + ($0.stars.map { ", \($0) stars" } ?? "") }
                            ?? "Open a review this rests on")
        .task {
            if let hit = Self.cache[reviewID] {
                who = hit
                return
            }
            guard let one: OneResponse = try? await APIClient.shared.send("/mobile/api/reviews/\(reviewID)",
                                                                          hapticOnError: false),
                  let r = one.review else { return }
            let first = (r.author ?? "").split(separator: " ").first.map(String.init) ?? ""
            let entry = (name: first.isEmpty ? "A guest" : first, stars: r.rating)
            Self.cache[reviewID] = entry
            who = entry
        }
    }
}
