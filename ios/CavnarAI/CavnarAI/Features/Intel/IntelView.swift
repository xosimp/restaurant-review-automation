import SwiftUI

enum IntelSubTab: String, CaseIterable, Identifiable {
    case competitors = "Competitors"
    case aiVisibility = "Online"
    var id: String { rawValue }

    /// The tab a nav path's section opens on (nav.py: "intel",
    /// "intel/ai-visibility", "intel/competitors", "competitor/<x>"; the
    /// website card lives beside AI visibility). Nil for no section.
    init?(section: String?) {
        guard let raw = section?.lowercased().replacingOccurrences(of: "_", with: "-"), !raw.isEmpty else {
            return nil
        }
        switch raw {
        case "ai-visibility", "aivisibility", "visibility", "aivis", "ai", "website", "web", "web-analytics":
            self = .aiVisibility
        case "intel", "competitors", "competitor", "movement", "recs", "market":
            self = .competitors
        default:
            // competitor/<place id or name> arrives with the target as the
            // section: it is about a competitor.
            self = .competitors
        }
    }
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
    @Environment(\.horizontalSizeClass) private var sizeClass
    /// Where the link that opened this was pointing (ModuleRoute's section
    /// and item): the AI-visibility tab, or a competitor opened in the list.
    let focusSection: String?
    let focusItem: String?
    @State private var focusApplied = false

    init(focusSection: String? = nil, focusItem: String? = nil) {
        self.focusSection = focusSection
        self.focusItem = focusItem
        _subTab = State(initialValue: IntelSubTab(section: focusSection) ?? .competitors)
    }
    @State private var expandedCompetitors: Set<String> = []
    /// Recommendations whose evidence and Ask link are open.
    @State private var expandedRecs: Set<String> = []
    /// The market sections (doing well / poorly / price) opened.
    @State private var showingMarketRead = false
    /// Every competitor, not just the five nearest.
    @State private var showingAllCompetitors = false
    /// The notes on how far the ratings compare (caveat, staleness).
    @State private var showingRatingNotes = false
    @State private var showAddCompetitor = false
    // Removal is fast now (a cached-blob filter, not the full refresh job
    // add uses — see removeCompetitor's own doc comment), but even a ~1s
    // wait with zero feedback reads as "did my tap register?" — swaps the
    // tapped row's own xmark for a small spinner for that brief window.
    @State private var removingPlaceId: String?
    /// A competitor the owner stopped tracking, hidden at once and removed
    /// on the server only when the Undo window ends (DESIGN_SYSTEM §10,
    /// tier 1 — the web's cavUndoable). Leaving the screen inside the
    /// window keeps them, the safe side.
    @State private var pendingRemoval: Competitor?
    @State private var removalTask: Task<Void, Never>?
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
                            CachedDataNotice(text: viewModel.stalenessNotice)
                            if !summary.hasData {
                                emptyState
                            } else {
                                content(summary)
                            }
                        } else if viewModel.isLoading {
                            CavnarLoadingOrb().padding(.top, 60).frame(maxWidth: .infinity)
                        } else if let error = viewModel.errorMessage {
                            VStack(spacing: CavnarSpace.xs) {
                                Text(error).cavnarText(.body)
                                Button { Task { await viewModel.load() } } label: {
                                    Text("Retry").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                                }
                                .buttonStyle(.plain)
                            }
                            .padding(.top, 60)
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        AIVisibilitySection(viewModel: aiVisibilityViewModel, restaurantName: viewModel.summary?.restaurantName)
                    }
                }
                .padding(.horizontal, CavnarSpace.gutter)
                .padding(.vertical, CavnarSpace.xl)
            }
            .cavnarEmberRefreshable { await viewModel.load() }
        }
        .overlay(alignment: .bottom) {
            if let pending = pendingRemoval {
                HStack(spacing: 12) {
                    Text("Stopped tracking \(pending.name)")
                        .cavnarText(.label)
                        .lineLimit(1)
                    Button {
                        Haptic.light()
                        undoRemoval()
                    } label: {
                        Text("Undo")
                            .cavnarText(.label, color: .cavnarEmber2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 16)
                .background(Color.cavnarPaper2, in: Capsule())
                .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .padding(.horizontal, 20)
                .padding(.bottom, 18)
                .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .animation(.easeOut(duration: 0.2), value: pendingRemoval?.placeId)
        .onDisappear { undoRemoval() }
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
            applyFocus()
        }
        // Reopening the app after a while re-reads competitor intel rather
        // than showing an earlier load as current (audit 4.2).
        .refreshOnForeground(lastLoaded: viewModel.lastLoadedAt) { await viewModel.load() }
    }

    /// The route's focus, once the competitors have loaded: a competitor
    /// named by place id or name opens expanded.
    private func applyFocus() {
        guard !focusApplied else { return }
        focusApplied = true
        guard subTab == .competitors, let summary = viewModel.summary else { return }
        let keys = [focusItem, focusSection].compactMap { $0?.lowercased() }
        if let hit = summary.competitors.first(where: { c in
            keys.contains(c.placeId.lowercased()) || keys.contains(c.name.lowercased())
        }) {
            expandedCompetitors.insert(hit.id)
            // Past the five nearest: the list opens in full so it shows.
            if let i = Self.nearestFirst(summary.competitors).firstIndex(where: { $0.id == hit.id }),
               i >= Self.competitorsShown {
                showingAllCompetitors = true
            }
        }
    }

    /// A competitor's distance in the phone's own units — miles in the US,
    /// kilometres elsewhere, feet or metres when close — with Measurement's
    /// road usage, never a hard-coded "km".
    static func distanceText(meters: Int, locale: Locale = .current) -> String {
        Measurement(value: Double(meters), unit: UnitLength.meters)
            .formatted(.measurement(width: .abbreviated, usage: .road,
                                    numberFormatStyle: .number.precision(.fractionLength(0...1)))
                .locale(locale))
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
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
        }
        .frame(maxWidth: .infinity)
        .animation(.easeOut(duration: 0.3), value: viewModel.isRefreshing)
    }

    @ViewBuilder
    private func content(_ summary: IntelSummary) -> some View {
        statRow(summary, showsLine: !Self.showsAnswerCard(summary))
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

        if Self.showsAnswerCard(summary) {
            answerCard(summary)
                .opacity(contentAppeared ? 1 : 0)
                .offset(y: contentAppeared ? 0 : 20)
                .animation(.easeOut(duration: 0.5).delay(0.15), value: contentAppeared)
        }

        // The rating-comparison bars that stood here are gone (iOS
        // readability round, 10/8/26): they drew every competitor's rating
        // a second time, beside the list below that already carries it.

        if !summary.sections.isEmpty || summary.displayRecommendations.count > 1
            || summary.emptyRecommendationsNote != nil {
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

    // MARK: - The answer (re-audit I6)
    //
    // Where you stand and the one thing to do, on the answer card's anatomy:
    // the standing is the headline, the top recommendation the action with
    // its answer row, its confidence only when the server sent one, and
    // Cavnar AI's paragraph — greeting stripped — behind "See the evidence".
    // It used to open on that paragraph ("Hi Brian, here is your…") at
    // three lines with a "Read more" that never appeared at large type.

    static func showsAnswerCard(_ s: IntelSummary) -> Bool {
        !(s.intro ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !s.displayRecommendations.isEmpty
    }

    /// The standing as a sentence: the hero line, capitalised.
    static func standingHeadline(_ s: IntelSummary) -> String {
        let count = s.competitors.count
        let market = s.marketRating
            ?? (count > 0 ? s.competitors.reduce(0.0) { $0 + $1.rating } / Double(count) : nil)
        let line = heroLine(s, market: count > 0 ? market : nil, nearby: count)
        return line.prefix(1).uppercased() + line.dropFirst()
    }

    /// The model's paragraph without its greeting: "Hi Brian, here is your
    /// read…" → "Here is your read…". A bare "Brian, …" opening goes too.
    static func strippedGreeting(_ intro: String, ownerName: String?) -> String {
        var s = intro.trimmingCharacters(in: .whitespacesAndNewlines)
        if let r = s.range(of: #"^(hi|hello|hey|good (morning|afternoon|evening))\b[^,.!\n]{0,40}[,.!]\s*"#,
                           options: [.regularExpression, .caseInsensitive]) {
            s.removeSubrange(r)
        } else if let name = ownerName?.trimmingCharacters(in: .whitespaces), !name.isEmpty,
                  s.lowercased().hasPrefix(name.lowercased()) {
            let rest = s.dropFirst(name.count)
            if let first = rest.first, first == "," || first == "!" || first == "." {
                s = String(rest.dropFirst()).trimmingCharacters(in: .whitespaces)
            }
        }
        guard let first = s.first else { return intro }
        return first.uppercased() + s.dropFirst()
    }

    private func answerCard(_ summary: IntelSummary) -> some View {
        let top = summary.displayRecommendations.first
        let paragraph = (summary.intro?.isEmpty == false)
            ? Self.strippedGreeting(summary.intro ?? "", ownerName: summary.ownerName) : nil
        return CavnarAnswerCard(
            kicker: "Where you stand",
            headline: Self.standingHeadline(summary),
            headlineRole: .headline,
            confidence: top?.confidence.map {
                ConfidenceLine(confidence: $0, recKey: top?.key, surface: "intel", module: "intel")
            },
            detailLabel: "See the evidence"
        ) {
            if let top {
                (Text("Do this first: ").font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
                 + HomeMixedText.make(top.text, role: .body))
                    .lineSpacing(CavnarText.body.lineSpacing)
                    .fixedSize(horizontal: false, vertical: true)
                if let key = top.key {
                    RecAnswerRow(key: key, surface: "intel")
                }
            }
        } detail: {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if let paragraph {
                    CavnarMixedText(paragraph, role: .body, color: .cavnarInk2)
                }
                if let top {
                    recEvidence(top)
                }
            }
        }
    }

    // MARK: - Market analysis — well/poorly/recommendations read as one
    // continuous AI narrative, not three independent page sections, so
    // they share one soft ember-tinted panel and one continuous accent
    // bar down the left edge instead of each getting its own kicker.

    private func marketAnalysisGroup(_ summary: IntelSummary) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.l) {
            HStack(spacing: CavnarSpace.xs) {
                CavnarKicker("Cavnar AI\u{2019}s read of the market", icon: "sparkles")
                Spacer(minLength: 0)
                // The sections are the model's read of Google-selected
                // reviews (claim_kinds "sections": inferred) — J5.
                ClaimKindTag(kind: summary.claimKinds?["sections"])
            }

            // What else to do comes first (density #35) — the top line is
            // the answer card's action above (I6); the analysis follows.
            if summary.displayRecommendations.count > 1 {
                recommendationsSection(Array(summary.displayRecommendations.dropFirst()), firstNumber: 2)
            } else if let note = summary.emptyRecommendationsNote {
                // An empty list is explained, never left blank: held back
                // because the read carried something unverified, or
                // genuinely nothing worth doing this week.
                Text(note)
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // The doing-well / doing-poorly / price read is the evidence
            // behind the moves, so it waits behind one tap (re-audit I15);
            // the moves stay out.
            if !summary.sections.isEmpty {
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.22)) { showingMarketRead.toggle() }
                } label: {
                    HStack(spacing: CavnarSpace.xxs + 2) {
                        Text(showingMarketRead ? "Hide the market read" : "What the market is doing")
                            .cavnarText(.label, color: .cavnarEmber2)
                        Image(systemName: "chevron.down")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarEmber2)
                            .rotationEffect(.degrees(showingMarketRead ? 180 : 0))
                            .accessibilityHidden(true)
                        Spacer(minLength: 0)
                    }
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityValue(showingMarketRead ? "Expanded" : "Collapsed")
                if showingMarketRead {
                    ForEach(summary.sections) { section in
                        marketSection(section)
                    }
                    .transition(.opacity)
                }
            }
        }
        .padding(.vertical, CavnarSpace.m)
        .padding(.horizontal, CavnarSpace.l)
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

    // MARK: - Hero line — the owner's standing in one line (density #35)

    /// "4.4★ · 0.2 ahead of 6 nearby", in the standing's tone. It was three
    /// equal tiles (Tracked / Market avg / You) and the owner had to compare
    /// two of them to learn where they stood; "Tracked: 6" weighed the same
    /// as their own rating. The rating is the screen's one 40pt figure.
    /// `showsLine`: the standing sentence under the figure — off when the
    /// answer card below carries it as its headline (re-audit I6).
    private func statRow(_ summary: IntelSummary, showsLine: Bool = true) -> some View {
        let count = summary.competitors.count
        // Volume-weighted, computed server-side. The old figure was a flat
        // mean over competitor ratings, so a twelve-review venue counted as
        // much as a three-thousand-review one — an average of averages, not
        // a market average.
        let avgRating = summary.marketRating
            ?? (count > 0 ? summary.competitors.reduce(0.0) { $0 + $1.rating } / Double(count) : 0)
        let line = Self.heroLine(summary, market: count > 0 ? avgRating : nil, nearby: count)

        return VStack(spacing: 10) {
            if let own = summary.ownRating {
                // Coloured against the market ONLY when both numbers are
                // the same kind (ownRatingTone): an average over imported
                // reviews is not comparable to competitors' all-time Google
                // ratings.
                let tone = Self.ownRatingTone(summary, own: own, market: avgRating)
                VStack(spacing: 4) {
                    ratingText(own, numberSize: CavnarType.heroNumber, tone: tone)
                        .cavnarSensitive()
                    if showsLine {
                        // The label role, not a literal bold body (I13).
                        HomeMixedText.make(line, role: .label,
                                           color: tone == .cavnarInk ? .cavnarInk2 : tone)
                            .multilineTextAlignment(.center)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity)
                .accessibilityElement(children: .combine)
            } else if showsLine {
                HomeMixedText.make(line, role: .label, color: .cavnarInk2)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
            }
            // n and radius behind the standing, and its tie band (#38) — or
            // why there is none yet.
            if let basis = summary.standingLine != nil ? summary.standingBasis
                : summary.standingWhyNot.map({ "No standing yet \u{2014} " + $0 }) {
                HomeMixedText.make(basis, role: .caption)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity)
            }
        }
    }

    /// "0.2 ahead of 6 nearby" / "level with 6 nearby" from the server's
    /// own gap (I10) — only when it made the comparison. Otherwise the
    /// market figure, untoned, beside the count ("market avg 4.2★ · 6
    /// nearby"), never a verdict on two different kinds of number.
    static func heroLine(_ s: IntelSummary, market: Double?, nearby: Int) -> String {
        let who = "\(nearby) nearby"
        if s.standing != nil, let gap = s.ownVsMarket {
            if abs(gap) < 0.05 { return "level with \(who)" }
            return "\(String(format: "%.1f", abs(gap))) \(gap > 0 ? "ahead of" : "behind") \(who)"
        }
        if let market { return "market avg \(String(format: "%.1f", market))\u{2605} \u{00B7} \(who)" }
        return nearby > 0 ? "\(who) tracked" : "No nearby restaurants tracked yet"
    }

    /// The own-rating tile's colour: the server's standing tone when it made
    /// the comparison (I10), ink when it declined to (the two ratings are
    /// not the same kind); only an older server that sends no standing
    /// falls back to the client's own ±0.3★ rule.
    static func ownRatingTone(_ summary: IntelSummary, own: Double, market: Double) -> Color {
        if summary.standingTone != nil || summary.standing != nil {
            guard summary.standing != nil else { return .cavnarInk }
            return summary.standingTone?.color ?? .cavnarInk
        }
        guard summary.ratingsAreComparable else { return .cavnarInk }
        return own >= market ? .cavnarGreen : (own >= market - 0.3 ? .cavnarAmber : .cavnarRed)
    }

    /// The owner's rating to judge each competitor row against — or nil, and
    /// the rows say no "ahead"/"behind" and draw no green or red accent
    /// (re-audit I1). The same guard as the hero (ownRatingTone): the
    /// server's standing when it sent one, and nothing when it declined to
    /// compare; only an older server with no standing falls back to whether
    /// the two ratings are the same kind.
    static func comparableOwnRating(_ summary: IntelSummary) -> Double? {
        guard let own = summary.ownRating else { return nil }
        if summary.standingTone != nil || summary.standing != nil {
            return summary.standing != nil ? own : nil
        }
        return summary.ratingsAreComparable ? own : nil
    }

    /// Number at the given size, star smaller — was one Text with "%.1f★"
    /// formatting into a single font/size, which made the star render as
    /// big as the digits next to it. `starScale` keeps a small rating's
    /// star legible.
    private func ratingText(_ rating: Double, numberSize: CGFloat, tone: Color, starScale: CGFloat = 0.55) -> Text {
        Text(String(format: "%.1f", rating)).font(.cavnarNumber(numberSize, weight: 700)).foregroundStyle(tone)
            + Text(" ★").font(.cavnarNumber(numberSize * starScale, weight: 700)).foregroundStyle(tone)
    }

    // MARK: - What the market's doing (well/poorly sections)

    private static let bulletsShown = 2

    /// Each section carries its own colour end to end (kicker, icon, row
    /// tint) so "doing well" reads positive and "doing poorly" negative at
    /// a glance. Two bullets each, the rest behind "+n more" (iOS
    /// readability round, 10/8/26).
    private func marketSection(_ section: IntelSection) -> some View {
        let isGood = section.name.localizedCaseInsensitiveContains("well")
        let isBad = section.name.localizedCaseInsensitiveContains("poorly")
        // Price positioning is a fact about the market, not a verdict: its
        // own neutral treatment (M-28 — it used to be dropped entirely).
        let tone = isGood ? Color.cavnarGreen : (isBad ? Color.cavnarRed : Color.cavnarInk2)
        let textTone = isBad ? Color.cavnarRedText : tone
        let icon = isGood ? "checkmark" : (isBad ? "xmark" : "tag")

        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(section.name,
                         icon: isGood ? "arrow.up.right" : (isBad ? "arrow.down.right" : "dollarsign"),
                         tint: textTone)
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                ForEach(Array(section.bullets.prefix(Self.bulletsShown)), id: \.self) { bullet in
                    marketBullet(bullet, tone: tone, icon: icon)
                }
                if section.bullets.count > Self.bulletsShown {
                    CavnarMoreDisclosure(hiddenCount: section.bullets.count - Self.bulletsShown) {
                        ForEach(Array(section.bullets.dropFirst(Self.bulletsShown)), id: \.self) { bullet in
                            marketBullet(bullet, tone: tone, icon: icon)
                        }
                    }
                }
            }
        }
    }

    private func marketBullet(_ bullet: String, tone: Color, icon: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Image(systemName: icon)
                .font(.cavnar(.caption))
                .foregroundStyle(tone)
                .accessibilityHidden(true)
            Text(bullet)
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, CavnarSpace.xs)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Recommendations

    private static let recsShown = 3

    /// What to do — the loudest of the market-analysis sections. The top
    /// three, each with its answer row; the reviews it rests on and "Ask
    /// about this" are behind "See the evidence" (iOS readability round,
    /// 10/8/26: each carried four controls in a row).
    private func recommendationsSection(_ recommendations: [IntelRecommendation], firstNumber: Int = 1) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(firstNumber > 1 ? "More to do" : "What to do", icon: "bolt.fill")
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                ForEach(Array(recommendations.prefix(Self.recsShown).enumerated()), id: \.element.id) { index, rec in
                    recommendationRow(rec, number: index + firstNumber)
                }
                if recommendations.count > Self.recsShown {
                    CavnarMoreDisclosure(hiddenCount: recommendations.count - Self.recsShown) {
                        ForEach(Array(recommendations.dropFirst(Self.recsShown).enumerated()), id: \.element.id) { index, rec in
                            recommendationRow(rec, number: index + firstNumber + Self.recsShown)
                        }
                    }
                }
            }
        }
    }

    private func recommendationRow(_ rec: IntelRecommendation, number: Int) -> some View {
        let open = expandedRecs.contains(rec.id)
        let hasEvidence = !(rec.cites ?? []).isEmpty || rec.key != nil
        return HStack(alignment: .top, spacing: CavnarSpace.s) {
            Text("\(number)")
                .font(.cavnar(.caption))
                .foregroundStyle(.white)
                .frame(width: 22, height: 22)
                .background(Color.cavnarEmber)
                .clipShape(Circle())
                .shadow(color: Color.cavnarEmber.opacity(0.55), radius: 4, x: 0, y: 0)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                Text(rec.text)
                    .cavnarText(.body, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                // How sure, as the server measured it (re-audit I7). The
                // payload carries no expected outcome for an Intel line, so
                // none is shown — never one written on the phone.
                if let c = rec.confidence {
                    ConfidenceLine(confidence: c, recKey: rec.key, surface: "intel", module: "intel")
                }
                if let key = rec.key {
                    RecAnswerRow(key: key, surface: "intel")
                }
                if hasEvidence {
                    Button {
                        Haptic.selection()
                        withAnimation(.easeOut(duration: 0.2)) {
                            if open { expandedRecs.remove(rec.id) } else { expandedRecs.insert(rec.id) }
                        }
                        // Reading the reviews it rests on is evidence viewed (#38).
                        if !open, !(rec.cites ?? []).isEmpty {
                            RecEvidenceLog.viewed(key: rec.key, surface: "intel", module: "intel")
                        }
                    } label: {
                        HStack(spacing: CavnarSpace.xxs) {
                            Text(open ? "Hide the evidence" : "See the evidence")
                            Image(systemName: "chevron.down")
                                .rotationEffect(.degrees(open ? 180 : 0))
                                .accessibilityHidden(true)
                        }
                        .cavnarText(.label, color: .cavnarEmber2)
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityValue(open ? "Expanded" : "Collapsed")
                    if open {
                        recEvidence(rec)
                            .padding(.leading, CavnarSpace.xs)
                            .overlay(alignment: .leading) {
                                Rectangle().fill(Color.cavnarInk3.opacity(0.6)).frame(width: 1)
                            }
                            .transition(.opacity)
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, CavnarSpace.xs)
        .padding(.horizontal, 10)
        .background(Color.cavnarEmber.opacity(0.09))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    /// The reviews a recommendation rests on and "Ask about this" — under a
    /// row's "See the evidence", and in the answer card's.
    @ViewBuilder
    private func recEvidence(_ rec: IntelRecommendation) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if let cites = rec.cites, !cites.isEmpty {
                CavnarMixedText("Reviews this rests on (\(cites.count))", role: .label, color: .cavnarInk2)
                ForEach(cites) { cite in
                    citeRow(cite)
                }
            }
            if let key = rec.key {
                // The web's "Ask about this" on each Intel
                // recommendation: the same question, with the
                // recommendation (its rec key) as the screen.
                HomeAskLink(
                    question: "About this Intel recommendation: \(rec.text)",
                    screen: AskScreen(panel: "competitor", entityType: "rec", entityId: key)
                )
            }
        }
    }

    /// competitor · 4★ · 2 weeks ago — "text"
    private func citeRow(_ cite: IntelRecommendation.Cite) -> some View {
        var head = Text(cite.competitor ?? "A competitor").font(.cavnar(.label))
        if let rating = cite.rating {
            let stars = rating.truncatingRemainder(dividingBy: 1) == 0
                ? String(Int(rating)) : String(format: "%.1f", rating)
            head = head + Text(" \u{00B7} ") + Text("\(stars)\u{2605}").font(.cavnar(.figureS))
        }
        if let time = cite.time, !time.isEmpty {
            head = head + Text(" \u{00B7} ") + HomeMixedText.make(time, role: .secondary)
        }
        return VStack(alignment: .leading, spacing: 2) {
            head
                .font(.cavnar(.secondary))
                .foregroundStyle(Color.cavnarInk2)
            if let text = cite.text, !text.isEmpty {
                CavnarMixedText("\u{201C}\(text)\u{201D}", role: .secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: - Competitor list (hairline dividers + colored accent bar, no cards)

    private static let competitorsShown = 5

    /// The five nearest first (distance, then the server's order for the
    /// ones with none), the rest behind "Show all" (iOS readability round,
    /// 10/8/26).
    static func nearestFirst(_ competitors: [Competitor]) -> [Competitor] {
        competitors.enumerated().sorted { a, b in
            switch (a.element.distanceM, b.element.distanceM) {
            case let (x?, y?) where x != y: return x < y
            case (_?, nil): return true
            case (nil, _?): return false
            default: return a.offset < b.offset
            }
        }.map(\.element)
    }

    private func competitorsSection(_ summary: IntelSummary) -> some View {
        let all = Self.nearestFirst(summary.competitors.filter { $0.placeId != pendingRemoval?.placeId })
        let shown = showingAllCompetitors ? all : Array(all.prefix(Self.competitorsShown))
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack(spacing: CavnarSpace.xs) {
                Text("Nearby competitors").cavnarText(.headline)
                Spacer()
                addCompetitorLink
                refreshLink
            }
            if let error = viewModel.refreshError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
            if viewModel.isRefreshing {
                CavnarRadarSweep(size: 140, caption: "Re-reading the neighborhood")
                    .padding(.vertical, 10)
                    .transition(.opacity)
            }
            if CavnarLayout.isWide(sizeClass) {
                // Two columns on an iPad (#99), a hairline under every row
                // but the last pair's.
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 24, alignment: .top),
                                    GridItem(.flexible(), alignment: .top)], spacing: 0) {
                    ForEach(Array(shown.enumerated()), id: \.element.id) { index, c in
                        VStack(spacing: 0) {
                            competitorRow(c, ownRating: Self.comparableOwnRating(summary))
                            if index < shown.count - (shown.count.isMultiple(of: 2) ? 2 : 1) {
                                Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                                    .padding(.leading, 14)
                            }
                        }
                    }
                }
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(shown.enumerated()), id: \.element.id) { index, c in
                        competitorRow(c, ownRating: Self.comparableOwnRating(summary))
                        if index < shown.count - 1 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                                .padding(.leading, 14)
                        }
                    }
                }
            }
            if all.count > Self.competitorsShown {
                CavnarMoreToggle(hiddenCount: all.count - Self.competitorsShown, total: all.count,
                                 isExpanded: $showingAllCompetitors)
            }
            // How far these ratings compare, and how fresh they are — one
            // line, the sentences behind it (iOS readability round).
            ratingNotes(summary)
            if let movement = viewModel.movement {
                movementSection(movement)
            }
            // The market's history and your own rating over time are a
            // chart for the web; the phone keeps what changed this week.
            CavnarWebLinkRow(title: "Ratings over time", subtitle: "The market\u{2019}s average and yours, week by week",
                             path: "intel", actionLabel: "Open on the web")
            // How current the sources behind Intel are, from data health.
            DataHealthModuleBadge(module: "intel")
        }
    }

    /// "Last updated 10/6/26 · 2 notes on these ratings ›" — the comparison
    /// caveat and the staleness note behind one tap.
    @ViewBuilder
    private func ratingNotes(_ summary: IntelSummary) -> some View {
        let notes = [summary.ratingComparisonCaveat, summary.stalenessNote].compactMap { $0 }
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if let updatedAt = summary.updatedAt {
                updatedLabel(updatedAt)
            }
            if !notes.isEmpty {
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.2)) { showingRatingNotes.toggle() }
                } label: {
                    HStack(spacing: CavnarSpace.xxs) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(Color.cavnarAmber)
                            .accessibilityHidden(true)
                        Text(notes.count == 1 ? "A note on these ratings" : "\(notes.count) notes on these ratings")
                            .foregroundStyle(Color.cavnarInk2)
                        Image(systemName: "chevron.down")
                            .rotationEffect(.degrees(showingRatingNotes ? 180 : 0))
                            .foregroundStyle(Color.cavnarInk2)
                            .accessibilityHidden(true)
                        Spacer(minLength: 0)
                    }
                    .font(.cavnar(.secondary))
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityValue(showingRatingNotes ? "Expanded" : "Collapsed")
                if showingRatingNotes {
                    ForEach(notes, id: \.self) { note in
                        Text(note)
                            .cavnarText(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .padding(.top, CavnarSpace.xxs)
    }

    /// Tier 1 of the confirm-and-undo policy: gone from the list now, and
    /// removed on the server only if nobody taps Undo in the next 7 seconds
    /// (the web toast's window). A second removal commits the first at once.
    private func stopTracking(_ c: Competitor) {
        if let earlier = pendingRemoval, earlier.placeId != c.placeId {
            removalTask?.cancel()
            let id = earlier.placeId
            Task { await viewModel.removeCompetitor(placeId: id) }
        }
        pendingRemoval = c
        removalTask?.cancel()
        removalTask = Task { @MainActor in
            try? await Task.sleep(for: .seconds(7))
            guard !Task.isCancelled, pendingRemoval?.placeId == c.placeId else { return }
            removingPlaceId = c.placeId
            await viewModel.removeCompetitor(placeId: c.placeId)
            removingPlaceId = nil
            if pendingRemoval?.placeId == c.placeId { pendingRemoval = nil }
        }
    }

    private func undoRemoval() {
        removalTask?.cancel()
        removalTask = nil
        pendingRemoval = nil
    }

    // MARK: - What changed

    /// Who opened or closed nearby between the last two weekly checks, and
    /// the rating moves past normal ups and downs — the web's "What changed"
    /// under Nearby competitors. Before two checks exist it says so, never
    /// "nothing changed".
    private func movementSection(_ m: IntelMovement) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("What changed")
                .padding(.top, 10)
            if let from = m.comparedFrom {
                HomeMixedText.make(m.hasChanges
                                   ? "Since your check on \(CavnarDate.mdy(from)): new places that opened near you, places Google marks closed, and ratings that really moved"
                                   : "No new places, no closures, and no rating moved past normal ups and downs since \(CavnarDate.mdy(from))",
                                   role: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Openings and closings show once there are two weekly checks to compare.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(m.arrived) { p in movementRow(p.name, tag: "Newly opened", detail: nil) }
            ForEach(m.gone) { p in movementRow(p.name, tag: "Closed", detail: nil) }
            ForEach(Array(m.significant.prefix(3))) { mv in
                movementRow(mv.name, tag: nil,
                            detail: String(format: "%.1f → %.1f★", mv.ratingThen, mv.ratingNow)
                                + ((mv.reviewsAdded ?? 0) > 0 ? " · +\(mv.reviewsAdded!) reviews" : ""))
            }
        }
    }

    private func movementRow(_ name: String, tag: String?, detail: String?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Text(name).cavnarText(.label)
            if let tag {
                Text(tag)
                    .cavnarText(.tag, color: .cavnarEmber2)
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(Capsule().fill(Color.cavnarEmber.opacity(0.14)))
            }
            Spacer(minLength: 0)
            if let detail {
                HomeMixedText.make(detail, role: .secondary)
            }
        }
        .padding(.vertical, 6)
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
        // A provisional rating rests on a handful of reviews. Colouring the
        // row against it tells the owner they are behind a number that is
        // not yet a reputation.
        let provisional = c.ratingIsProvisional == true
        let accent: Color = {
            if provisional { return Color.cavnarPaper3 }
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
                    Text(c.name).cavnarText(.label)
                    if let basis = c.matchBasis, basis.contains("widened") {
                        Text("loose match")
                            .cavnarText(.tag)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(Color.cavnarPaper2))
                    }
                    if provisional {
                        // A rating on too few reviews to lean on — not a
                        // new restaurant (J10).
                        Text("few reviews")
                            .cavnarText(.tag)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(Color.cavnarPaper2))
                    }
                    // Only ever shown for an owner-added competitor — an
                    // auto-discovered one was never in custom_competitors,
                    // so there's nothing here for the client to remove
                    // that it didn't add itself (mirrors mobile_api.py's
                    // own remove-competitor route, which only ever
                    // touches that field).
                    if c.isCustom {
                        Button {
                            Haptic.light()
                            stopTracking(c)
                        } label: {
                            Group {
                                if removingPlaceId == c.placeId {
                                    CavnarShimmerLine(color: .cavnarEmber2)
                                        .frame(width: 14)
                                } else {
                                    Image(systemName: "xmark.circle.fill")
                                        .font(.cavnar(.body))
                                        .foregroundStyle(Color.cavnarInk2)
                                }
                            }
                            .cavnarHitTarget()
                        }
                        .disabled(removingPlaceId != nil)
                        .buttonStyle(.plain)
                        .accessibilityLabel("Stop tracking \(c.name)")
                    }
                    Spacer()
                    if let diff {
                        Text(diff == 0 ? "tied" : (diff > 0 ? "▲\(String(format: "%.1f", diff)) ahead" : "▼\(String(format: "%.1f", abs(diff))) behind"))
                            .cavnarText(.label, color: diff > 0 ? Color.cavnarGreen : (diff < 0 ? Color.cavnarRedText : Color.cavnarInk2))
                    }
                }
                HStack(spacing: 6) {
                    ratingText(c.rating, numberSize: CavnarType.secondary, tone: Color.cavnarAmber, starScale: 0.8)
                    Text("\(c.reviewCount) reviews")
                        .cavnarText(.secondary)
                    if !c.vicinity.isEmpty {
                        Text("· \(c.vicinity)")
                            .cavnarText(.secondary)
                            .lineLimit(1)
                    }
                    // How far away, when we know. A competitor selected on
                    // the widened pass can be five miles out and used to
                    // read exactly like one across the street.
                    // In the owner's own units (miles for a US phone),
                    // through Measurement — it was always m / km.
                    if let m = c.distanceM, m > 0 {
                        Text("\u{00B7} \(Self.distanceText(meters: m))")
                            .font(.cavnarNumber(CavnarType.secondary))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                    if c.isCustom {
                        Text("· Added by you")
                            .cavnarText(.secondary, color: .cavnarEmber2)
                    }
                }
                ForEach(visibleReviews) { r in
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: "star.fill")
                            .font(.cavnar(.caption))
                            .foregroundStyle(r.rating >= 4 ? Color.cavnarGreen : Color.cavnarRed)
                            .accessibilityHidden(true)
                        Text(r.text)
                            .cavnarText(.secondary)
                            .lineLimit(isExpanded ? nil : 2)
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
                            .cavnarText(.label, color: .cavnarEmber2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
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
                // M/D/YY, the one owner-facing date form (was M/D/YYYY).
                display = CavnarDate.mdy(datePart)
            }
        }
        return HStack(spacing: CavnarSpace.xs) {
            HomeMixedText.make("Last updated \(display)", role: .secondary)
            if let daysOld, daysOld >= 7 {
                Text("Worth a refresh")
                    .cavnarText(.tag, color: .cavnarAmber)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(Color.cavnarAmberBg)
                    .clipShape(Capsule())
            }
        }
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
            HStack(spacing: CavnarSpace.xxs) {
                Image(systemName: "plus").font(.cavnar(.caption))
                Text("Add")
            }
            .font(.cavnar(.label))
            .foregroundStyle(Color.cavnarEmber2)
            .padding(.horizontal, 11)
            .padding(.vertical, 5)
            .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.4), lineWidth: 1))
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Add a competitor")
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
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarEmber2)
                    .cavnarHitTarget()
            } else {
                HStack(spacing: CavnarSpace.xxs) {
                    Image(systemName: "arrow.clockwise").font(.cavnar(.caption))
                    Text("Refresh")
                }
                .font(.cavnar(.label))
                .foregroundStyle(Color.cavnarEmber2)
                .padding(.horizontal, 11)
                .padding(.vertical, 5)
                .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.4), lineWidth: 1))
                .cavnarHitTarget()
            }
        }
        .buttonStyle(.plain)
        .disabled(viewModel.isRefreshing)
    }
}
