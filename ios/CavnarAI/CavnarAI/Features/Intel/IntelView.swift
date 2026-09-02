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
    @State private var showAddCompetitor = false
    // Removal is fast now (a cached-blob filter, not the full refresh job
    // add uses — see removeCompetitor's own doc comment), but even a ~1s
    // wait with zero feedback reads as "did my tap register?" — swaps the
    // tapped row's own xmark for a small spinner for that brief window.
    @State private var removingPlaceId: String?
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
                            CavnarLoadingSeal().padding(.top, 60).frame(maxWidth: .infinity)
                        } else if let error = viewModel.errorMessage {
                            VStack(spacing: 8) {
                                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                                Button("Retry") { Task { await viewModel.load() } }
                            }
                            .padding(.top, 60)
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        AIVisibilitySection(viewModel: aiVisibilityViewModel, restaurantName: viewModel.summary?.restaurantName)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 24)
            }
            .cavnarEmberRefreshable { await viewModel.load() }
        }
        .cavnarModuleBackground()
        .sheet(isPresented: $showAddCompetitor) {
            AddCompetitorSheet(viewModel: viewModel)
        }
        .navigationTitle("Intel")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Intel") }
        // Replaces the plain cavnarEmberBackButton() — owns the back
        // chevron itself (tap dismisses on Competitors, returns to
        // Competitors first from AI Visibility) so it can also add the
        // swipe gesture, same convention as Food Cost/Labor/Marketing's
        // own sub-tabs.
        .cavnarTabSwipeNavigation($subTab, primaryTab: .competitors, secondaryTab: .aiVisibility)
        .task {
            await viewModel.load()
        }
    }

    /// "Cold Hearth" while there's nothing here (the CTA is what lights
    /// the ember), swapped for "Reading the Room" — a radar finding
    /// competitors as ember blips — for the ~30s the fetch job runs.
    private var emptyState: some View {
        VStack(spacing: 10) {
            if viewModel.isRefreshing {
                CavnarRadarSweep(caption: "Scanning nearby restaurants")
                    .padding(.top, 50)
                    .transition(.opacity)
            } else {
                CavnarEmptyHearth(
                    title: "No competitor data yet",
                    message: "See how your ratings, review volume, and reputation stack up against nearby restaurants. Takes about 30 seconds.",
                    ctaLabel: "Fetch competitor data"
                ) {
                    Task { await viewModel.refreshCompetitors() }
                }
                .padding(.top, 16)
                .transition(.opacity)
            }
            if let error = viewModel.refreshError {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
            }
        }
        .frame(maxWidth: .infinity)
        .animation(.easeOut(duration: 0.3), value: viewModel.isRefreshing)
    }

    @ViewBuilder
    private func content(_ summary: IntelSummary) -> some View {
        statRow(summary)
            .opacity(contentAppeared ? 1 : 0)
            .offset(y: contentAppeared ? 0 : 20)
            .animation(.easeOut(duration: 0.5), value: contentAppeared)
            // Flips once this real content has actually rendered its first
            // (hidden) frame — onAppear fires strictly after that commit,
            // unlike the previous DispatchQueue.main.async race against the
            // .task's own continuation. That race could coalesce into the
            // SAME transaction once anything else in the view tree (like
            // CavnarEmberRefreshable's scroll-geometry tracking) added
            // enough extra work to shift timing — collapsing the "hidden"
            // and "shown" frames into one and silently skipping every fade/
            // rise/bar-fill animation below (the actual regression this
            // fixes: stat row, hero insight, and the rating comparison bars
            // stopped animating in once refreshable was added to this screen).
            .onAppear {
                guard !contentAppeared else { return }
                contentAppeared = true
            }

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
                // Continues the same fade/rise sequence statRow (0s) and
                // heroInsight (.15s delay) already use — this and
                // competitorsSection below never had it at all, which is
                // why everything from here down just appeared instantly
                // while the sections above it were still visibly animating.
                .opacity(contentAppeared ? 1 : 0)
                .offset(y: contentAppeared ? 0 : 20)
                .animation(.easeOut(duration: 0.5).delay(0.45), value: contentAppeared)
        }

        if !summary.competitors.isEmpty {
            competitorsSection(summary)
                .opacity(contentAppeared ? 1 : 0)
                .offset(y: contentAppeared ? 0 : 20)
                .animation(.easeOut(duration: 0.5).delay(0.6), value: contentAppeared)
        }
    }

    // MARK: - Hero insight — this is the AI's own opening line, and it's
    // meant to be the one sentence on this page that actually grabs you,
    // not unattributed body copy sitting quietly at the top. No kicker,
    // no icon, no accent bar — just size and the owner's name in the
    // brand's own ember, same treatment Home's own greeting headline uses
    // for the exact same reason (HomeView.heroHeadline).

    private func heroInsight(_ intro: String, ownerName: String?) -> some View {
        // The real AI prompt opens with "Hi {name}, here is your..." — a
        // plain hasPrefix(ownerName) check misses it since the sentence
        // starts with "Hi ", not the name itself. Search a short leading
        // window instead of anchoring to the very first character, so
        // "Hi Brian," and a bare "Brian," (this screen's demo copy before
        // real API access) both highlight correctly — capped at 20 chars
        // so a name that happens to reappear later in a long insight
        // doesn't get matched instead.
        let leadingWindow = intro.prefix(20)
        let nameRange = ownerName.flatMap { name -> Range<String.Index>? in
            guard !name.isEmpty else { return nil }
            return leadingWindow.range(of: name)
        }

        return Group {
            if let nameRange {
                (Text(String(intro[intro.startIndex..<nameRange.lowerBound])).foregroundStyle(Color.cavnarInk)
                    + Text(String(intro[nameRange])).foregroundStyle(Color.cavnarEmber)
                    + Text(String(intro[nameRange.upperBound...])).foregroundStyle(Color.cavnarInk))
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
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1.1)
            }
            .foregroundStyle(Color.cavnarEmber)

            ForEach(summary.sections) { section in
                marketSection(section)
            }

            if !summary.recommendations.isEmpty {
                recommendationsSection(summary.recommendations)
            }
        }
        .padding(.vertical, 18)
        .padding(.horizontal, 22)
        // Was 0.12 — at that strength the panel's own orange wash blended
        // into the "doing poorly" section's red row tint right on top of
        // it, making the two hard to tell apart. Dimmed so the panel reads
        // as a quiet backdrop again and the red/green row tints do the
        // actual contrast work, with the left accent bar (unchanged, still
        // the strongest of the three) as the panel's own visual anchor.
        .background(Color.cavnarEmber.opacity(0.07))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.cavnarEmber.opacity(0.2), lineWidth: 1)
        )
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.cavnarEmber.opacity(0.6)).frame(width: 2.5)
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
                .font(.cavnarBody(13.5, weight: 700))
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
        VStack(alignment: .leading, spacing: 18) {
            Text("RATING COMPARISON")
                .font(.cavnarBody(14, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber)
            VStack(spacing: 16) {
                ratingBar(name: summary.restaurantName ?? "Your restaurant", rating: ownRating, tone: Color.cavnarEmber, isYou: true)
                // Was green/red per competitor based on ahead-or-behind —
                // stacked next to the client's own ember bar, that read as
                // too many colors at once ("kiddish"). Every competitor
                // bar is the same neutral gray now; only the client's own
                // restaurant keeps the brand color, so it's the one thing
                // that actually stands out.
                ForEach(summary.competitors) { c in
                    ratingBar(name: c.name, rating: c.rating, tone: Color.cavnarInk3, isYou: false)
                }
            }
        }
        .opacity(contentAppeared ? 1 : 0)
        .offset(y: contentAppeared ? 0 : 20)
        .animation(.easeOut(duration: 0.5).delay(0.3), value: contentAppeared)
    }

    /// Was a flat gray capsule for every competitor regardless of how they
    /// actually compare — no color, no depth, nothing to look at twice.
    /// Now: a real gradient fill with a matching glow (green/red by
    /// whether you're ahead of THIS specific competitor, same logic the
    /// competitor rows below already use), a thicker/brighter treatment
    /// for your own bar so it reads as the anchor of the comparison, and
    /// the fill animates in from zero width on load instead of just
    /// appearing static.
    private func ratingBar(name: String, rating: Double, tone: Color, isYou: Bool) -> some View {
        HStack(spacing: 12) {
            Text(name)
                .font(.cavnarBody(isYou ? 13 : 12, weight: isYou ? 700 : 500))
                .foregroundStyle(isYou ? Color.cavnarInk : Color.cavnarInk2)
                .lineLimit(1)
                .frame(width: 104, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.5))
                    Capsule()
                        .fill(
                            LinearGradient(
                                colors: [tone.opacity(0.6), tone],
                                startPoint: .leading, endPoint: .trailing
                            )
                        )
                        .frame(width: geo.size.width * (contentAppeared ? min(rating / 5, 1) : 0))
                        .shadow(color: tone.opacity(0.65), radius: isYou ? 7 : 4, x: 0, y: 0)
                }
            }
            .frame(height: isYou ? 14 : 10)
            .animation(.easeOut(duration: 0.7).delay(0.4), value: contentAppeared)
            // Bar fill already counted up from 0 on load; the number next
            // to it just appeared at full value instantly, which read as
            // inconsistent once you were watching the bar animate right
            // beside it. Same trigger, same timing as the bar above so
            // they land together.
            Group {
                if isYou {
                    AnimatedRatingText(rating: contentAppeared ? rating : 0, numberSize: 15, tone: tone).cavnarNumberGlow(tone)
                } else {
                    AnimatedRatingText(rating: contentAppeared ? rating : 0, numberSize: 12, tone: tone)
                }
            }
            .frame(width: 42, alignment: .trailing)
            .animation(.easeOut(duration: 0.7).delay(0.4), value: contentAppeared)
        }
    }

    /// Same count-up-once technique as DesignSystem's CavnarAnimatableNumber,
    /// but building ratingText's own two-segment Text (bigger digits,
    /// smaller star) each frame instead of a single formatted string — that
    /// dual-font-size treatment needs to survive every intermediate
    /// animated frame, not just the final value, which a String-based
    /// formatter can't carry. Kept local to this one call site rather than
    /// generalizing CavnarAnimatableNumber itself, since nowhere else needs
    /// a two-segment animated number.
    private struct AnimatedRatingText: View, Animatable {
        var rating: Double
        let numberSize: CGFloat
        let tone: Color

        var animatableData: Double {
            get { rating }
            set { rating = newValue }
        }

        var body: some View {
            Text(String(format: "%.1f", rating)).font(.cavnarNumber(numberSize, weight: 700)).foregroundStyle(tone)
                + Text(" ★").font(.cavnarNumber(numberSize * 0.55, weight: 700)).foregroundStyle(tone)
        }
    }

    // MARK: - What the market's doing (well/poorly sections)

    /// Well/poorly used to share one flat treatment — ember kicker, gray
    /// body text, and only a tiny checkmark/xmark icon told them apart.
    /// Now each carries its own color end to end (kicker, icon, per-row
    /// tint) so "doing well" reads unmistakably positive and "doing
    /// poorly" unmistakably negative at a glance, not just on close read.
    private func marketSection(_ section: IntelSection) -> some View {
        let isGood = section.name.localizedCaseInsensitiveContains("well")
        let tone = isGood ? Color.cavnarGreen : Color.cavnarRed
        let icon = isGood ? "checkmark" : "xmark"

        return VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 6) {
                Image(systemName: isGood ? "arrow.up.right" : "arrow.down.right")
                    .font(.system(size: 9, weight: .bold))
                Text(section.name.uppercased())
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1.2)
            }
            .foregroundStyle(tone)
            VStack(alignment: .leading, spacing: 10) {
                ForEach(section.bullets, id: \.self) { bullet in
                    HStack(alignment: .top, spacing: 10) {
                        ZStack {
                            Circle().fill(tone.opacity(0.2)).frame(width: 18, height: 18)
                            Image(systemName: icon)
                                .font(.system(size: 8.5, weight: .bold))
                                .foregroundStyle(tone)
                        }
                        .padding(.top, 1)
                        Text(bullet)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk2)
                            .lineSpacing(3)
                    }
                    .padding(.vertical, 8)
                    .padding(.horizontal, 10)
                    .background(tone.opacity(0.07))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }

    // MARK: - Recommendations

    /// Deliberately the loudest of the three market-analysis sections — a
    /// solid (not outlined) glowing number badge and a stronger row tint
    /// than well/poorly's bullets, so these read as the actionable next
    /// steps rather than more descriptive analysis in the same voice.
    private func recommendationsSection(_ recommendations: [String]) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 6) {
                Image(systemName: "bolt.fill")
                    .font(.system(size: 9, weight: .bold))
                Text("HOW TO IMPROVE")
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1.2)
            }
            .foregroundStyle(Color.cavnarEmber)
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(recommendations.enumerated()), id: \.offset) { index, rec in
                    HStack(alignment: .top, spacing: 10) {
                        Text("\(index + 1)")
                            .font(.cavnarNumber(14, weight: 700))
                            .foregroundStyle(.white)
                            .frame(width: 20, height: 20)
                            .background(Color.cavnarEmber)
                            .clipShape(Circle())
                            .shadow(color: Color.cavnarEmber.opacity(0.55), radius: 4, x: 0, y: 0)
                        Text(rec)
                            .font(.cavnarBody(14.5, weight: 500))
                            .foregroundStyle(Color.cavnarInk)
                            .lineSpacing(3)
                    }
                    .padding(.vertical, 8)
                    .padding(.horizontal, 10)
                    .background(Color.cavnarEmber.opacity(0.09))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }

    // MARK: - Competitor list (hairline dividers + colored accent bar, no cards)

    private func competitorsSection(_ summary: IntelSummary) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("NEARBY COMPETITORS")
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber)
                Spacer()
                addCompetitorLink
                refreshLink
            }
            if let error = viewModel.refreshError {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
            }
            if viewModel.isRefreshing {
                CavnarRadarSweep(size: 140, caption: "Re-reading the neighborhood")
                    .padding(.vertical, 10)
                    .transition(.opacity)
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
        // Google returns these in its own "most relevant" order, which
        // mixes positive and negative reviews together — grouping by
        // rating (highest first, stable within a tie) reads as one clean
        // block of praise followed by one clean block of complaints
        // instead of bouncing between green and red stars line to line.
        let sortedReviews = c.reviews.sorted { $0.rating > $1.rating }
        let visibleReviews = isExpanded ? sortedReviews : Array(sortedReviews.prefix(1))
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
            // Explicitly kills animation on this subtree for isExpanded
            // changes — without this, ScrollView's own implicit content-
            // resize animation was leaking in, making the "show more"
            // button's label visibly drop and fade as the new review rows
            // pushed it down instead of just instantly relaying out.
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text(c.name)
                        .font(.cavnarBody(15, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    // Only ever shown for an owner-added competitor — an
                    // auto-discovered one was never in custom_competitors,
                    // so there's nothing here for the client to remove
                    // that it didn't add itself (mirrors mobile_api.py's
                    // own remove-competitor route, which only ever
                    // touches that field).
                    if c.isCustom {
                        Button {
                            Haptic.light()
                            Task {
                                removingPlaceId = c.placeId
                                await viewModel.removeCompetitor(placeId: c.placeId)
                                removingPlaceId = nil
                            }
                        } label: {
                            if removingPlaceId == c.placeId {
                                CavnarShimmerLine(color: .cavnarEmber2)
                                    .frame(width: 14)
                            } else {
                                Image(systemName: "xmark.circle.fill")
                                    .font(.system(size: 12))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        .disabled(removingPlaceId != nil)
                        .buttonStyle(.plain)
                    }
                    Spacer()
                    if let diff {
                        Text(diff == 0 ? "tied" : (diff > 0 ? "▲\(String(format: "%.1f", diff)) ahead" : "▼\(String(format: "%.1f", abs(diff))) behind"))
                            .font(.cavnarBody(13.5, weight: 700))
                            .foregroundStyle(diff > 0 ? Color.cavnarGreen : (diff < 0 ? Color.cavnarRed : Color.cavnarInk3))
                    }
                }
                HStack(spacing: 6) {
                    ratingText(c.rating, numberSize: 11, tone: Color.cavnarAmber)
                    Text("\(c.reviewCount) reviews")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                    if !c.vicinity.isEmpty {
                        Text("· \(c.vicinity)")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                            .lineLimit(1)
                    }
                    if c.isCustom {
                        Text("· Added by you")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                }
                ForEach(visibleReviews) { r in
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: "star.fill")
                            .font(.system(size: 8))
                            .foregroundStyle(r.rating >= 4 ? Color.cavnarGreen : Color.cavnarRed)
                            .padding(.top, 2)
                        Text(r.text)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .lineLimit(2)
                            .lineSpacing(2)
                    }
                    .padding(.top, 2)
                }
                if c.reviews.count > 1 {
                    Button {
                        // .animation(nil, value:) below wasn't enough on its
                        // own — the button's own label was still visibly
                        // dropping and fading as the newly-revealed review
                        // rows pushed it down. disablesAnimations is the
                        // actual hard override: it suppresses animation for
                        // this state mutation regardless of any ambient/
                        // inherited transaction (e.g. ScrollView's own
                        // implicit content-resize animation), where
                        // .animation(nil, value:) alone only overrides
                        // animation attributed to this one value's own change.
                        var transaction = Transaction(animation: nil)
                        transaction.disablesAnimations = true
                        withTransaction(transaction) {
                            if isExpanded {
                                expandedCompetitors.remove(c.id)
                            } else {
                                expandedCompetitors.insert(c.id)
                            }
                        }
                    } label: {
                        Text(isExpanded ? "Show less" : "Show \(remaining) more review\(remaining == 1 ? "" : "s")")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .padding(.top, 1)
                }
            }
            .animation(nil, value: isExpanded)
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
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
            if let daysOld, daysOld >= 7 {
                Text("Consider refreshing")
                    .font(.cavnarBody(13.5, weight: 700))
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
                CavnarShimmerText(text: "Refreshing…")
            } else {
                Text(label)
            }
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
        .disabled(viewModel.isRefreshing)
    }

    /// Same small-pill language as refreshLink right beside it — this is
    /// the one new control this feature needed, so it reuses an already-
    /// established visual pattern on this exact page rather than
    /// introducing a new button style or a separate section just for it.
    private var addCompetitorLink: some View {
        Button {
            Haptic.light()
            showAddCompetitor = true
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "plus")
                    .font(.system(size: 9, weight: .bold))
                Text("Add")
            }
        }
        .font(.cavnarBody(14, weight: 700))
        .foregroundStyle(Color.cavnarEmber2)
        .padding(.horizontal, 11)
        .padding(.vertical, 5)
        .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.4), lineWidth: 1))
    }

    /// Small pill next to the "NEARBY COMPETITORS" kicker — quiet enough
    /// not to compete with the unboxed page for attention, but still
    /// reads as a tappable control rather than plain text sitting there.
    private var refreshLink: some View {
        Button {
            Haptic.light()
            Task { await viewModel.refreshCompetitors() }
        } label: {
            if viewModel.isRefreshing {
                PulsingText("Refreshing…")
            } else {
                HStack(spacing: 4) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 9, weight: .bold))
                    Text("Refresh")
                }
            }
        }
        .font(.cavnarBody(14, weight: 700))
        .foregroundStyle(Color.cavnarEmber2)
        .padding(.horizontal, 11)
        .padding(.vertical, 5)
        .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.4), lineWidth: 1))
        .disabled(viewModel.isRefreshing)
    }
}
