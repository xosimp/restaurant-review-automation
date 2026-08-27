import SwiftUI

private enum IntelSubTab: String, CaseIterable, Identifiable {
    case competitors = "Competitors"
    case aiVisibility = "AI Visibility"
    var id: String { rawValue }
}

/// Competitors tab is deliberately unboxed — no .cavnarCard() walls anywhere
/// on this screen — matching the same direction Food Cost/Labor's own
/// Analytics tabs already took (see FoodCostAnalyticsSection's doc comment:
/// "built around whitespace and typography instead of stacking bordered
/// card after bordered card"). Sections signal themselves with a kicker
/// label and generous vertical spacing; grouped items use a hairline
/// divider or a colored left-edge accent bar instead of a box.
struct IntelView: View {
    @State private var viewModel = IntelViewModel()
    @State private var aiVisibilityViewModel = AIVisibilityViewModel()
    @State private var subTab: IntelSubTab = .competitors
    @State private var expandedCompetitors: Set<String> = []
    // Drives the initial-load reveal — stats fade/rise into place first,
    // the AI insight follows a beat after (see content(_:)'s two .delay
    // values below). Tied to the data load finishing, not view-appear, so
    // it plays once per real load rather than replaying every time you
    // swipe back to this sub-tab.
    @State private var contentAppeared = false

    var body: some View {
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: IntelSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 16)

            ScrollView {
                VStack(alignment: .leading, spacing: 32) {
                    if subTab == .competitors {
                        if let summary = viewModel.summary {
                            if !summary.hasData {
                                emptyState
                            } else {
                                content(summary)
                            }
                        } else if viewModel.isLoading {
                            ProgressView().padding(.top, 60)
                        } else if let error = viewModel.errorMessage {
                            VStack(spacing: 8) {
                                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                                Button("Retry") { Task { await viewModel.load() } }
                            }
                            .padding(.top, 60)
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        AIVisibilitySection(viewModel: aiVisibilityViewModel)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 24)
            }
        }
        .cavnarModuleBackground()
        .refreshable { await viewModel.load() }
        .navigationTitle("Intel")
        .navigationBarTitleDisplayMode(.inline)
        // Replaces the plain cavnarEmberBackButton() — owns the back
        // chevron itself (tap dismisses on Competitors, returns to
        // Competitors first from AI Visibility) so it can also add the
        // swipe gesture, same convention as Food Cost/Labor/Marketing's
        // own sub-tabs.
        .cavnarTabSwipeNavigation($subTab, primaryTab: .competitors, secondaryTab: .aiVisibility)
        .task {
            await viewModel.load()
            contentAppeared = true
        }
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            Image(systemName: "binoculars")
                .font(.system(size: 32))
                .foregroundStyle(Color.cavnarInk3)
            Text("No competitor data yet")
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text("See how your ratings, review volume, and reputation stack up against nearby restaurants. Takes about 30 seconds.")
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
            refreshButton(label: "Fetch competitor data")
            if let error = viewModel.refreshError {
                Text(error).font(.cavnarBody(11)).foregroundStyle(Color.cavnarRed)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    @ViewBuilder
    private func content(_ summary: IntelSummary) -> some View {
        statRow(summary)
            .opacity(contentAppeared ? 1 : 0)
            .offset(y: contentAppeared ? 0 : 20)
            .animation(.easeOut(duration: 0.5), value: contentAppeared)

        if let intro = summary.intro, !intro.isEmpty {
            heroInsight(intro, ownerName: summary.ownerName)
                .opacity(contentAppeared ? 1 : 0)
                .offset(y: contentAppeared ? 0 : 20)
                .animation(.easeOut(duration: 0.5).delay(0.15), value: contentAppeared)
        }

        if let ownRating = summary.ownRating, !summary.competitors.isEmpty {
            ratingComparisonSection(summary, ownRating: ownRating)
        }

        if !summary.sections.isEmpty || !summary.recommendations.isEmpty {
            marketAnalysisGroup(summary)
        }

        if !summary.competitors.isEmpty {
            competitorsSection(summary)
        }
    }

    // MARK: - Hero insight — this is the AI's own opening line, and it's
    // meant to be the one sentence on this page that actually grabs you,
    // not unattributed body copy sitting quietly at the top. No kicker,
    // no icon, no accent bar — just size and the owner's name in the
    // brand's own ember, same treatment Home's own greeting headline uses
    // for the exact same reason (HomeView.heroHeadline).

    private func heroInsight(_ intro: String, ownerName: String?) -> some View {
        Group {
            if let ownerName, !ownerName.isEmpty, intro.hasPrefix(ownerName) {
                (Text(ownerName).foregroundStyle(Color.cavnarEmber2)
                    + Text(String(intro.dropFirst(ownerName.count))).foregroundStyle(Color.cavnarInk))
            } else {
                Text(intro).foregroundStyle(Color.cavnarInk)
            }
        }
        .font(.cavnarHeadline(22))
        .lineSpacing(5)
    }

    // MARK: - Market analysis — well/poorly/recommendations read as one
    // continuous AI narrative, not three independent page sections, so
    // they share one soft ember-tinted panel and one continuous accent
    // bar down the left edge instead of each getting its own kicker.

    private func marketAnalysisGroup(_ summary: IntelSummary) -> some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 6) {
                Image(systemName: "sparkles")
                    .font(.system(size: 10, weight: .semibold))
                Text("CAVNAR AI COMPETITIVE ANALYSIS")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.1)
            }
            .foregroundStyle(Color.cavnarEmber2)

            ForEach(summary.sections) { section in
                marketSection(section)
            }

            if !summary.recommendations.isEmpty {
                recommendationsSection(summary.recommendations)
            }
        }
        .padding(.vertical, 18)
        .padding(.horizontal, 22)
        .background(Color.cavnarEmber.opacity(0.06))
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.cavnarEmber.opacity(0.55)).frame(width: 2.5)
        }
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }

    // MARK: - Summary stat row (bare — no card, just numbers + hairline dividers)

    private func statRow(_ summary: IntelSummary) -> some View {
        let count = summary.competitors.count
        let avgRating = count > 0 ? summary.competitors.reduce(0.0) { $0 + $1.rating } / Double(count) : 0

        return HStack(spacing: 0) {
            statTile(value: plainStatValue("\(count)"), label: "Tracked")
            Rectangle().fill(Color.cavnarPaper3).frame(width: 1, height: 28)
            statTile(
                value: count > 0 ? ratingText(avgRating, numberSize: 20, tone: Color.cavnarInk) : plainStatValue("—"),
                label: "Market avg"
            )
            Rectangle().fill(Color.cavnarPaper3).frame(width: 1, height: 28)
            if let own = summary.ownRating {
                // Colored relative to the competitor average, not an
                // absolute threshold — a 4.1 that's genuinely ahead of a
                // weak local market should read as good news exactly as
                // much as a 4.1 that's behind a strong one should read as
                // a gap to close. An absolute >=4.0-is-green cutoff doesn't
                // know which of those it's looking at, and the competitor
                // row accents below already use this same relative logic —
                // this tile was the one place on the page disagreeing
                // with itself.
                statTile(
                    value: ratingText(own, numberSize: 20, tone: own >= avgRating ? Color.cavnarGreen : (own >= avgRating - 0.3 ? Color.cavnarAmber : Color.cavnarRed)),
                    label: summary.restaurantName ?? "Your rating"
                )
            } else {
                statTile(value: plainStatValue("\(summary.recommendations.count)"), label: "Action items")
            }
        }
    }

    /// value arrives already fully styled (font + color on every segment)
    /// — this only lays it out, it never applies its own font/color on
    /// top, since a later blanket modifier here would silently win over
    /// per-segment styling set by ratingText below (SwiftUI resolves
    /// Text-concatenation styling closest-to-the-literal-segment-wins, so
    /// stacking a second, outer style call is never safe to rely on).
    private func statTile(value: Text, label: String) -> some View {
        VStack(spacing: 5) {
            value
            Text(label.uppercased())
                .font(.cavnarBody(8.5, weight: 700))
                .tracking(0.4)
                .foregroundStyle(Color.cavnarInk3)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity)
    }

    private func plainStatValue(_ s: String) -> Text {
        Text(s).font(.cavnarNumber(20, weight: 700)).foregroundStyle(Color.cavnarInk)
    }

    /// Number at the given size, star noticeably smaller — was one Text
    /// with "%.1f★" formatting into a single font/size, which made the
    /// star render as big as the digits next to it.
    private func ratingText(_ rating: Double, numberSize: CGFloat, tone: Color) -> Text {
        Text(String(format: "%.1f", rating)).font(.cavnarNumber(numberSize, weight: 700)).foregroundStyle(tone)
            + Text(" ★").font(.cavnarNumber(numberSize * 0.55, weight: 700)).foregroundStyle(tone)
    }

    // MARK: - Rating comparison

    private func ratingComparisonSection(_ summary: IntelSummary, ownRating: Double) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("RATING COMPARISON")
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 12) {
                ratingBar(name: "Your restaurant", rating: ownRating, tone: Color.cavnarEmber2, boldName: true)
                ForEach(summary.competitors) { c in
                    ratingBar(name: c.name, rating: c.rating, tone: Color.cavnarPaper3, boldName: false)
                }
            }
        }
    }

    private func ratingBar(name: String, rating: Double, tone: Color, boldName: Bool) -> some View {
        HStack(spacing: 12) {
            Text(name)
                .font(.cavnarBody(12, weight: boldName ? 700 : 400))
                .foregroundStyle(boldName ? Color.cavnarInk : Color.cavnarInk2)
                .lineLimit(1)
                .frame(width: 96, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                    Capsule().fill(tone)
                        .frame(width: geo.size.width * min(rating / 5, 1))
                }
            }
            .frame(height: 7)
            Text(String(format: "%.1f", rating))
                .font(.cavnarNumber(12, weight: 700))
                .foregroundStyle(boldName ? Color.cavnarEmber2 : Color.cavnarInk2)
                .frame(width: 28, alignment: .trailing)
        }
    }

    // MARK: - What the market's doing (well/poorly sections)

    private func marketSection(_ section: IntelSection) -> some View {
        let isGood = section.name.localizedCaseInsensitiveContains("well")
        let tone = isGood ? Color.cavnarGreen : Color.cavnarRed
        let icon = isGood ? "checkmark" : "xmark"

        return VStack(alignment: .leading, spacing: 14) {
            Text(section.name.uppercased())
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(alignment: .leading, spacing: 12) {
                ForEach(section.bullets, id: \.self) { bullet in
                    HStack(alignment: .top, spacing: 10) {
                        ZStack {
                            Circle().fill(tone.opacity(0.16)).frame(width: 16, height: 16)
                            Image(systemName: icon)
                                .font(.system(size: 8, weight: .bold))
                                .foregroundStyle(tone)
                        }
                        .padding(.top, 1)
                        Text(bullet)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk2)
                            .lineSpacing(3)
                    }
                }
            }
        }
    }

    // MARK: - Recommendations

    private func recommendationsSection(_ recommendations: [String]) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("HOW TO IMPROVE")
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(alignment: .leading, spacing: 14) {
                ForEach(Array(recommendations.enumerated()), id: \.offset) { index, rec in
                    HStack(alignment: .top, spacing: 10) {
                        Text("\(index + 1)")
                            .font(.cavnarNumber(11, weight: 700))
                            .foregroundStyle(Color.cavnarEmber)
                            .frame(width: 18, height: 18)
                            .background(Color.cavnarEmber.opacity(0.14))
                            .clipShape(Circle())
                        Text(rec)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk)
                            .lineSpacing(3)
                    }
                }
            }
        }
    }

    // MARK: - Competitor list (hairline dividers + colored accent bar, no cards)

    private func competitorsSection(_ summary: IntelSummary) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("NEARBY COMPETITORS")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                Spacer()
                refreshLink
            }
            if let error = viewModel.refreshError {
                Text(error).font(.cavnarBody(11)).foregroundStyle(Color.cavnarRed)
            }
            VStack(spacing: 0) {
                ForEach(Array(summary.competitors.enumerated()), id: \.element.id) { index, c in
                    competitorRow(c, ownRating: summary.ownRating)
                    if index < summary.competitors.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                            .padding(.leading, 14)
                    }
                }
            }
            if let updatedAt = summary.updatedAt {
                updatedLabel(updatedAt)
            }
        }
    }

    private func competitorRow(_ c: Competitor, ownRating: Double?) -> some View {
        let isExpanded = expandedCompetitors.contains(c.id)
        let visibleReviews = isExpanded ? c.reviews : Array(c.reviews.prefix(2))
        let remaining = c.reviews.count - visibleReviews.count
        let diff = ownRating.map { ((($0) - c.rating) * 10).rounded() / 10 }
        let accent: Color = {
            guard let diff else { return Color.cavnarPaper3 }
            if diff > 0 { return Color.cavnarGreen }
            if diff < 0 { return Color.cavnarRed }
            return Color.cavnarInk3
        }()

        return HStack(alignment: .top, spacing: 12) {
            Rectangle().fill(accent).frame(width: 2.5)
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text(c.name)
                        .font(.cavnarBody(13.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    Spacer()
                    if let diff {
                        Text(diff == 0 ? "tied" : (diff > 0 ? "▲\(String(format: "%.1f", diff)) ahead" : "▼\(String(format: "%.1f", abs(diff))) behind"))
                            .font(.cavnarBody(9, weight: 700))
                            .foregroundStyle(diff > 0 ? Color.cavnarGreen : (diff < 0 ? Color.cavnarRed : Color.cavnarInk3))
                    }
                }
                HStack(spacing: 6) {
                    ratingText(c.rating, numberSize: 11, tone: Color.cavnarAmber)
                    Text("\(c.reviewCount) reviews")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                    if !c.vicinity.isEmpty {
                        Text("· \(c.vicinity)")
                            .font(.cavnarBody(11))
                            .foregroundStyle(Color.cavnarInk3)
                            .lineLimit(1)
                    }
                }
                ForEach(visibleReviews) { r in
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: "star.fill")
                            .font(.system(size: 8))
                            .foregroundStyle(r.rating >= 4 ? Color.cavnarGreen : Color.cavnarRed)
                            .padding(.top, 2)
                        Text(r.text)
                            .font(.cavnarBody(11.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .lineLimit(2)
                            .lineSpacing(2)
                    }
                    .padding(.top, 2)
                }
                if remaining > 0 {
                    Button {
                        expandedCompetitors.insert(c.id)
                    } label: {
                        Text("Show \(remaining) more review\(remaining == 1 ? "" : "s")")
                            .font(.cavnarBody(11, weight: 600))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .padding(.top, 1)
                }
            }
        }
        .padding(.vertical, 14)
    }

    private func updatedLabel(_ updatedAt: String) -> some View {
        let datePart = String(updatedAt.prefix(10))
        let parts = datePart.split(separator: "-").compactMap { Int($0) }
        var daysOld: Int? = nil
        var display = datePart
        if parts.count == 3 {
            var comps = DateComponents()
            comps.year = parts[0]; comps.month = parts[1]; comps.day = parts[2]
            if let date = Calendar.current.date(from: comps) {
                daysOld = Calendar.current.dateComponents([.day], from: date, to: Date()).day
                display = "\(parts[1])/\(parts[2])/\(parts[0])"
            }
        }
        return HStack(spacing: 8) {
            Text("Last updated \(display)")
                .font(.cavnarBody(11))
                .foregroundStyle(Color.cavnarInk3)
            if let daysOld, daysOld >= 7 {
                Text("Consider refreshing")
                    .font(.cavnarBody(9, weight: 700))
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(Color.cavnarAmberBg)
                    .clipShape(Capsule())
            }
        }
        .padding(.top, 4)
    }

    /// Prominent CTA — the empty state's only action on screen.
    private func refreshButton(label: String) -> some View {
        Button {
            Task { await viewModel.refreshCompetitors() }
        } label: {
            if viewModel.isRefreshing {
                HStack(spacing: 6) {
                    ProgressView().tint(.white)
                    Text("Refreshing…")
                }
            } else {
                Text(label)
            }
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
        .disabled(viewModel.isRefreshing)
    }

    /// Small pill next to the "NEARBY COMPETITORS" kicker — quiet enough
    /// not to compete with the unboxed page for attention, but still
    /// reads as a tappable control rather than plain text sitting there.
    private var refreshLink: some View {
        Button {
            Task { await viewModel.refreshCompetitors() }
        } label: {
            if viewModel.isRefreshing {
                HStack(spacing: 6) {
                    ProgressView().tint(Color.cavnarEmber2)
                    Text("Refreshing…")
                }
            } else {
                HStack(spacing: 4) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 9, weight: .bold))
                    Text("Refresh")
                }
            }
        }
        .font(.cavnarBody(10.5, weight: 700))
        .foregroundStyle(Color.cavnarEmber2)
        .padding(.horizontal, 11)
        .padding(.vertical, 5)
        .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.4), lineWidth: 1))
        .disabled(viewModel.isRefreshing)
    }
}
