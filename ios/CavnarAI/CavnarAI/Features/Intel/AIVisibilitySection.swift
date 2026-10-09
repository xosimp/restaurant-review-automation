import SwiftUI

/// One roadmap action card. `title`/`actionLabel`/`impact` stay fixed —
/// which 4 categories exist and how they rank against each other is
/// legitimate general product guidance, not something that needs to vary
/// per restaurant. `detail` and `why` are now built per-restaurant from
/// this restaurant's own real numbers (see roadmapSection's detail/why
/// builder functions) — this used to be 4 fully static strings identical
/// for every restaurant regardless of where they actually stood, which is
/// exactly what read as "generic SEO advice" rather than a real roadmap.
///
/// The server now builds these cards (`roadmap` on the payload,
/// client_api.ai_visibility_roadmap — same copy, order and done rules as
/// the web) and keys each one, so the phone renders the server's cards
/// with Done / Not for us under the open ones. The local builder below is
/// only the fallback for an older server that sends no `roadmap`.
private struct RoadmapCard: Identifiable {
    let id: String
    let color: Color
    let title: String
    let detail: String
    let why: String
    let actionLabel: String
    let impact: String
    let done: Bool
    let action: () -> Void
    /// The server card's rec_ledger key, and whether it can be answered.
    var recKey: String? = nil
    var answerable: Bool = false
}

struct AIVisibilitySection: View {
    let viewModel: AIVisibilityViewModel
    // Comes from the sibling Competitors tab's own already-loaded summary
    // (IntelView shares one restaurant across both sub-tabs) — nil only in
    // the brief window before that load completes, in which case the
    // pre-check headline falls back to "your restaurant" rather than
    // showing a blank or broken sentence.
    var restaurantName: String?
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    @State private var showGbpChecklist = false
    @State private var expandedWhy: Set<String> = []
    /// The hero's range, background, recall and summary lines.
    @State private var showHeroDetails = false

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxl) {
            if viewModel.result == nil || viewModel.result?.isNotMeasured == true {
                preCheckHero
                storedState
                checkButton
            } else if let result = viewModel.result {
                if !result.ok {
                    Text(result.error ?? "Couldn't check AI visibility.")
                        .cavnarText(.body, color: .cavnarRedText)
                    checkButton
                } else {
                    heroPanel(result)
                    if showGbpChecklist, let checklist = result.checklist {
                        gbpChecklistGrid(checklist)
                    }
                    if let queries = result.queries {
                        queriesSection(queries, demand: result.searchDemand)
                    }
                    if let checklist = result.checklist {
                        roadmapSection(result, checklist: checklist)
                    }
                    // A Check that did not run leaves the recorded one on
                    // screen and says so here (re-audit P8).
                    if let err = viewModel.checkError {
                        Text(err)
                            .cavnarText(.body, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    checkButton
                }
            }
            // The restaurant's own website — Google Analytics and Search
            // Console, read every morning (parity audit 10/7/26 #52). The
            // tab is "Online" now (iOS readability round, 10/8/26): the
            // website was never AI visibility.
            WebsiteAnalyticsSection()
        }
        // The recorded check first (re-audit P1: a GET, never a live run),
        // then the history the Orbit draws.
        .task {
            await viewModel.loadStored()
            await viewModel.loadHistory()
            await viewModel.loadQueryHistory()
        }
    }

    /// Under the pre-check hero: the recorded check loading, or the server's
    /// "not measured yet" — never a 0 (re-audit P1).
    @ViewBuilder
    private var storedState: some View {
        if viewModel.isLoadingStored {
            CavnarShimmerLine()
                .frame(width: 120)
        } else if let reason = viewModel.notMeasuredReason ?? viewModel.result?.reason?.value {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Image(systemName: "clock")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk2)
                    .accessibilityHidden(true)
                Text("Not measured yet. \(reason)")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var checkButton: some View {
        VStack(spacing: 24) {
            Button {
                Task { await viewModel.check() }
            } label: {
                if viewModel.isChecking {
                    VStack(spacing: 6) {
                        CavnarShimmerText(text: "Checking…")
                        // Ember2 (the brighter accent), not white — stays on
                        // brand as an orange line while still reading clearly
                        // against the button's own solid Ember background.
                        CavnarShimmerLine(color: .cavnarEmber2)
                            .frame(width: 120)
                    }
                } else {
                    Text(viewModel.result == nil ? "Check my AI visibility" : "Re-run")
                }
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(viewModel.isChecking)

            // "Reading the Room" — the same radar the Competitors tab uses
            // for its fetch, here for the ~10s of live AI queries.
            if viewModel.isChecking {
                CavnarRadarSweep(size: 150, caption: "Querying AI assistants")
                    .transition(.opacity)
            }
        }
        // The button itself stays hug-content sized — this centers that
        // hug-content button within the full width instead of letting the
        // parent's .leading-aligned VStack pin it to the left edge.
        .frame(maxWidth: .infinity)
        .animation(.easeOut(duration: 0.3), value: viewModel.isChecking)
    }

    // MARK: - Pre-check hero — this screen used to be one bare button
    // floating over a black void until the first check ran. Explains what
    // the check actually does and previews the three things it returns,
    // so there's something to read/anticipate before tapping, not just an
    // unexplained button with no context for what it's about to do.

    private var preCheckHero: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xl) {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                Text("Is \(restaurantName ?? "your restaurant") visible to AI search?")
                    .cavnarText(.headline)
                // Precise about what actually runs. The old wording —
                // "asking ChatGPT, Perplexity and Google AI … this checks
                // whether you show up in those answers" — reads as a claim
                // that all three are queried. Only Perplexity is (see
                // client_api's ai-visibility check), so the copy now says
                // so and explains why that stands in for the rest.
                // No claim about what ChatGPT or Google AI read, or about how
                // guests search: neither is sourced, and the page's own
                // footer says what an assistant answers from is not
                // something we can see (NS1 #9).
                Text("This runs real guest-style queries through one AI search system \u{2014} Perplexity's live web search \u{2014} and shows whether you came up. What any AI assistant answers from is its own business, not something we can see.")
                    .cavnarText(.body)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 16) {
                previewRow(
                    icon: "text.bubble.fill", tone: Color.cavnarEmber,
                    title: "What AI said, by question",
                    detail: "The exact questions real guests ask AI tools, and whether your restaurant came up."
                )
                previewRow(
                    icon: "checklist", tone: Color.cavnarGreen,
                    title: "Listing strength",
                    detail: "What's missing from your Google Business Profile that AI tools pull answers from."
                )
                previewRow(
                    icon: "map.fill", tone: Color.cavnarBlue,
                    title: "A personalized roadmap",
                    detail: "Ranked, concrete next steps for your restaurant — not generic SEO advice."
                )
            }
        }
    }

    private func previewRow(icon: String, tone: Color, title: String, detail: String) -> some View {
        HStack(alignment: .top, spacing: CavnarSpace.s) {
            ZStack {
                RoundedRectangle(cornerRadius: 8).fill(tone.opacity(0.16)).frame(width: 32, height: 32)
                Image(systemName: icon).font(.cavnar(.secondary)).foregroundStyle(tone)
            }
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).cavnarText(.label)
                Text(detail).cavnarText(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - Hero — the score ring, the two figures and one status line
    // (iOS readability round, 10/8/26). The range, the background note,
    // branded recall, who else was named, the setup count and the computed
    // summary are behind "Details": it used to stack up to seven caveat
    // lines under the two figures.

    private func heroPanel(_ result: AIVisibilityResult) -> some View {
        // `result.aiScore ?? 0` rendered a MISSING measurement as zero, and
        // aiScoreLabel(0) reads "Not yet indexed by AI search" in red. A
        // Perplexity outage, or a restaurant with no city on file, was being
        // shown to the owner as a verdict on their business. The backend has
        // always returned partial/location_known/answered_queries saying
        // exactly this; neither client decoded them.
        let measured = result.scoreIsMeasured
        let scoreText: String = {
            guard let s = result.aiScore else { return "—" }
            if let lo = result.aiScoreLow, let hi = result.aiScoreHigh, hi > lo {
                return "\(lo)–\(hi)%"
            }
            return "\(s)%"
        }()
        return VStack(alignment: .leading, spacing: 0) {
            // The Orbit: today's score as a ring, every past run as a line.
            // A missing score is "not measured" on the ring too — never a
            // 0% ring (J10).
            VisibilityOrbitChart(score: result.aiScore, runs: viewModel.history,
                                 low: result.aiScoreLow, high: result.aiScoreHigh,
                                 band: measured ? result.aiChipText : "An estimate, not a measurement")
                .padding(.bottom, CavnarSpace.s)
                .opacity(measured ? 1 : 0.6)
            HStack(spacing: 0) {
                heroStat(
                    value: scoreText,
                    // The server's chip, read from the 90% range (I4); the
                    // point breakpoints only for an older server.
                    tone: measured ? (result.aiScoreTone?.color ?? aiScoreTone(result.aiScore ?? 0)) : Color.cavnarInk2,
                    // Named, not "AI". One system is asked.
                    label: result.platform ?? "AI",
                    sub: measured ? (result.aiChipText ?? aiScoreLabel(result.aiScore ?? 0)) : "Not measured",
                    claim: result.aiScore == nil ? nil : result.claimKinds?["ai_score"]
                )
                Rectangle().fill(Color.cavnarEmber.opacity(0.3)).frame(width: 1).padding(.vertical, 6)
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.2)) { showGbpChecklist.toggle() }
                } label: {
                    // presence_score covers the restaurant's real public
                    // listing and review record. gbp_score used to blend that
                    // with whether a Yelp ID had been typed into Cavnar AI,
                    // which is this product's configuration, not the
                    // restaurant's standing anywhere.
                    // nil when nothing on the listing could be read: a dash,
                    // never 0%. Items that need the Google listing are left
                    // out of the score while it can't be read (M-14).
                    let listing = result.presenceScore ?? result.gbpScore
                    heroStat(
                        value: listing.map { "\($0)%" } ?? "\u{2014}",
                        // The server's tone when it sends one (I10: one
                        // source for the thresholds); the client's own
                        // breakpoints only for an older server.
                        tone: listing.map { Self.presenceColor(server: result.presenceTone, score: $0) } ?? Color.cavnarInk2,
                        label: result.presenceHeading,
                        // The band in the server's words (I10), one table
                        // with the tone above.
                        sub: listing.map { (result.presenceChipText ?? gbpScoreLabel($0)) + ((result.presenceUnmeasured ?? 0) > 0
                                                               ? " \u{00B7} of \(result.presenceMeasured ?? 0) read" : "") }
                            ?? "Not measured",
                        expandable: true, isExpanded: showGbpChecklist,
                        claim: listing == nil ? nil : result.claimKinds?["presence_score"]
                    )
                }
                .buttonStyle(.plain)
            }
            heroStatusLine(result)
                .padding(.top, CavnarSpace.s)
            heroDetails(result)
        }
        .padding(CavnarSpace.l)
        .background(
            LinearGradient(
                colors: [Color.cavnarEmber.opacity(0.5), Color.cavnarEmber.opacity(0.08)],
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

    /// The one status line: why the number is not a measurement when it is
    /// not one (amber), else when the check ran.
    @ViewBuilder
    private func heroStatusLine(_ result: AIVisibilityResult) -> some View {
        if result.scoreCaveat != nil {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarAmber)
                    .accessibilityHidden(true)
                Text("An estimate, not a measurement \u{2014} why is in Details")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else if let measured = result.measuredLine {
            CavnarMixedText(measured, role: .secondary)
        }
    }

    /// Everything else the check says, behind one tap.
    private func heroDetails(_ result: AIVisibilityResult) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.2)) { showHeroDetails.toggle() }
            } label: {
                HStack(spacing: CavnarSpace.xxs) {
                    Text(showHeroDetails ? "Hide details" : "Details")
                    Image(systemName: "chevron.down")
                        .rotationEffect(.degrees(showHeroDetails ? 180 : 0))
                        .accessibilityHidden(true)
                    Spacer(minLength: 0)
                }
                .cavnarText(.label, color: .cavnarEmber2)
                .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityValue(showHeroDetails ? "Expanded" : "Collapsed")
            if showHeroDetails {
                VStack(alignment: .leading, spacing: CavnarSpace.s) {
                    // Why this number is not a measurement, when it is not one.
                    if let caveat = result.scoreCaveat {
                        Text(caveat).cavnarText(.secondary).fixedSize(horizontal: false, vertical: true)
                        if let measured = result.measuredLine {
                            CavnarMixedText(measured, role: .secondary)
                        }
                    } else if let lo = result.aiScoreLow, let hi = result.aiScoreHigh,
                              let a = result.answeredQueries, hi > lo {
                        CavnarMixedText("Across \(a) questions. The range is how precise that sample can be.", role: .secondary)
                    }
                    // Once the check is a week old, the same "background"
                    // rule Intel's competitors use (#35).
                    if let note = result.backgroundNote() {
                        CavnarMixedText(note, role: .secondary, color: .cavnarAmber)
                    }
                    // Branded recall and competitor appearance — a number
                    // computed and never shown is a number nobody can act on.
                    if let b = result.brandedScore, (result.brandedQueries ?? 0) > 0 {
                        Text(b >= 50
                             ? "Asked about you by name, \(result.platform ?? "it") recognised you."
                             : "Asked about you by name, \(result.platform ?? "it") didn't recognise you. That's separate from whether you come up in an open search.")
                            .cavnarText(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let comps = result.competitorAppearances, !comps.isEmpty {
                        Text("Also named in these answers: "
                             + comps.prefix(3).map { "\($0.name) (\($0.queries))" }.joined(separator: ", ")
                             + (comps.count > 3 ? " and \(comps.count - 3) more" : ""))
                            .cavnarText(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let done = result.setupDone, let total = result.setupTotal, total > 0 {
                        Text("\(done) of \(total) Cavnar AI connections set up. These help us read your listing; they don't change what AI search sees.")
                            .cavnarText(.caption)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let insight = heroInsightText(result) {
                        insight
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .transition(.opacity)
            }
        }
        .padding(.top, CavnarSpace.xxs)
    }

    /// value arrives pre-styled by the caller (glow tint differs per stat) —
    /// this never applies its own color on top of what's passed in.
    private func heroStat(value: String, tone: Color, label: String, sub: String, expandable: Bool = false,
                          isExpanded: Bool = false, claim: String? = nil) -> some View {
        VStack(spacing: CavnarSpace.xxs) {
            Text(value)
                .font(.cavnar(.figureM))
                .foregroundStyle(tone)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
                .cavnarNumberGlow(tone)
            HStack(spacing: 3) {
                Text(label).cavnarText(.label, color: .cavnarInk2)
                if expandable {
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarInk2)
                        .accessibilityHidden(true)
                }
            }
            // Mixed: a range chip ("somewhere between 30% and 90%…") keeps
            // its figures in Space Grotesk.
            HomeMixedText.make(sub, role: .secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            // Measured, or a partial check's estimate (claim_kinds) — J5.
            ClaimKindTag(kind: claim)
        }
        .frame(maxWidth: .infinity)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }

    /// Computed client-side from real fields (appearedCount/totalQueries/
    /// checklist) — there's no AI-written summary sentence in the API
    /// response the way Intel's competitor insight has one, so this reads
    /// straight off the actual numbers rather than inventing prose.
    ///
    /// Returns Text (not String) so the two numbers — how many queries
    /// appeared in, out of how many total — can carry their own bigger,
    /// ember-colored style and stay visually distinct from the surrounding
    /// prose. Per-segment .font()/.foregroundStyle() set here survives the
    /// blanket .font()/.foregroundStyle() the call site still applies to
    /// the whole composed Text — same technique platformCard/contactsGrid
    /// elsewhere in this app already rely on for "make this number pop."
    private func heroInsightText(_ result: AIVisibilityResult) -> Text? {
        // appeared_count is the OPEN questions only, so it is out of
        // answered_queries (7), never total_queries (8): "Tell me about
        // <name>" names the restaurant and is recall, not discovery (10/7/26).
        let appeared = result.appearedCount ?? 0
        let total = result.answeredQueries ?? result.totalQueries ?? 0
        guard total > 0 else { return nil }
        if appeared == 0 {
            // AI answers these from other sites; the ones it read are listed
            // under each question (10/7/26 — more reviews and a complete
            // Google listing were the advice, and the answers showed AI
            // reading directories, review sites and local lists instead).
            return Text("Not yet appearing in AI search — normal for independent restaurants this early. AI answers from ")
                + highlightedPhrase("other sites")
                + Text(": directories, review sites and local lists. The ones it read are under each question below; ")
                + highlightedPhrase("getting onto them")
                + Text(" is what moves this.")
        }
        var text = Text("Appears in ")
            + highlightedNumber(appeared)
            + Text(" of ")
            + highlightedNumber(total)
            + Text(" open AI search questions.")
        if let topGap = (result.checklist ?? []).filter({ !$0.done }).max(by: { $0.pts < $1.pts }) {
            text = text + Text(" \(topGap.action) is the fastest way to close the gap.")
        }
        return text
    }

    private func highlightedNumber(_ value: Int) -> Text {
        Text("\(value)")
            .font(.cavnar(.figureS))
            .foregroundStyle(Color.cavnarEmber2)
    }

    /// The named thing the owner actually has to go do — set bigger and in
    /// ember against the 14.5pt Ink2 prose the call site applies.
    private func highlightedPhrase(_ phrase: String) -> Text {
        Text(phrase)
            .font(.cavnar(.label))
            .foregroundStyle(Color.cavnarEmber2)
    }

    private func aiScoreLabel(_ score: Int) -> String {
        // Was "Not yet indexed by AI search", which is a claim about every
        // AI system from a sample of one.
        if score >= 67 { return "Comes up often" }
        if score >= 34 { return "Comes up sometimes" }
        return "Doesn't come up yet"
    }

    // Same breakpoints as aiScoreLabel above (34/67) — was a flat
    // Ember2 regardless of score, so a 0% and a 100% read identically.
    private func aiScoreTone(_ score: Int) -> Color {
        if score >= 67 { return .cavnarGreen }
        if score >= 34 { return .cavnarAmber }
        return .cavnarRed
    }

    private func gbpScoreLabel(_ score: Int) -> String {
        if score >= 80 { return "Excellent" }
        if score >= 60 { return "Good — a few gaps" }
        if score >= 40 { return "Needs work" }
        return "Critical gaps"
    }

    private static func gbpTone(_ score: Int) -> Color {
        if score >= 70 { return .cavnarGreen }
        if score >= 40 { return .cavnarAmber }
        return .cavnarRed
    }

    /// The listing-strength colour: the server's `presence_tone` (good /
    /// warn / bad) when sent, else the local breakpoints.
    static func presenceColor(server: ServerTone?, score: Int) -> Color {
        server?.color ?? gbpTone(score)
    }

    // MARK: - Listing strength — one column (iOS readability round,
    // 10/8/26): two columns truncated every item's action to one line.

    private func gbpChecklistGrid(_ checklist: [AIVisibilityChecklistItem]) -> some View {
        let doneCount = checklist.filter(\.done).count
        // Open gaps first, then what is done.
        let ordered = checklist.filter { !$0.done } + checklist.filter(\.done)
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack {
                CavnarKicker("LISTING STRENGTH")
                Spacer()
                CavnarMixedText("\(doneCount) of \(checklist.count) done", role: .secondary)
            }
            VStack(spacing: 0) {
                ForEach(ordered) { item in
                    gbpGridItem(item)
                }
            }
        }
    }

    private func gbpGridItem(_ item: AIVisibilityChecklistItem) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Image(systemName: item.done ? "checkmark.circle.fill" : (item.needsGmb ? "lock.fill" : "circle"))
                .font(.cavnar(.secondary))
                .foregroundStyle(item.done ? Color.cavnarGreen : Color.cavnarInk2)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xxs) {
                    Text(item.label)
                        .cavnarText(.label, color: item.done ? .cavnarInk2 : .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("+\(item.pts)").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                }
                if !item.done {
                    Text(item.needsGmb ? item.action + " (needs your Google listing)" : item.action)
                        .cavnarText(.secondary, color: .cavnarEmber2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, CavnarSpace.xs)
        .accessibilityElement(children: .combine)
        .accessibilityValue(item.done ? "Done" : "Open")
        .overlay(alignment: .bottom) {
            Rectangle().fill(Color.cavnarPaper3).frame(height: 1)
        }
    }

    // MARK: - AI query results — "Missed 5 of 7 questions" and the
    // questions it missed, each tapped open for the answer (iOS readability
    // round, 10/8/26: the full answer was press-and-hold only). Every
    // question and answer is on the web.

    private func queriesSection(_ queries: [AIVisibilityQuery], demand: AIVisibilitySearchDemand?) -> some View {
        let missed = queries.filter { !$0.appeared }
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Latest check")
            CavnarMixedText(missed.isEmpty
                            ? "Came up in all \(queries.count) questions"
                            : "Missed \(missed.count) of \(queries.count) question\(queries.count == 1 ? "" : "s")",
                            role: .lead)
            if let demand, let n = demand.questions, n > 0 {
                googleVsAIText(demand, questions: n)
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(spacing: 0) {
                ForEach(Array(missed.enumerated()), id: \.element.id) { index, q in
                    queryRow(q)
                    if index < missed.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                    }
                }
            }
            CavnarWebLinkRow(title: "Every question and answer", subtitle: "What AI said each time, and the sites it read",
                             path: "intel", actionLabel: "Open on the web")
        }
    }

    private func queryRow(_ q: AIVisibilityQuery) -> some View {
        QueryResultRow(q: q, history: viewModel.queryHistory?.question(for: q.query),
                       runs: viewModel.queryHistory?.runs ?? [])
    }

    /// Google vs AI (10/7/26): the Google searches put to AI, and the share of
    /// their search volume whose answer named the restaurant.
    private func googleVsAIText(_ d: AIVisibilitySearchDemand, questions: Int) -> Text {
        Text("Google vs AI: ")
            + Text("\(questions)").font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk)
            + Text(" of these questions are searches where Google showed your website (")
            + Text((d.impressions ?? 0).formatted()).font(.cavnarNumber(CavnarType.secondary, weight: 500))
            + Text(" times in 28 days). AI named you on ")
            + Text(d.coveredPct.map { "\($0)%" } ?? "\u{2014}").font(.cavnarNumber(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarInk)
            + Text(" of that search volume.")
    }

    // MARK: - Roadmap

    private func roadmapSection(_ result: AIVisibilityResult, checklist: [AIVisibilityChecklistItem]) -> some View {
        // Matched against client_api.py's checklist copy directly (verified,
        // not guessed) — two real bugs found here:
        //   "review" alone also matches the done=true label for the
        //   RESPONSE-rate item ("Excellent review response rate (X%)"),
        //   so a restaurant with a great response rate but under 50
        //   reviews could false-positive this card as done. "google
        //   review" only appears on the review-COUNT item's own two
        //   labels ("50+ Google reviews" / "Build to 50+ Google reviews
        //   (...)"), never on the response-rate item's.
        //   "respond" never matches at all: the only done=true label for
        //   that category is "Excellent review response rate (X%)", which
        //   contains "response" but not "respond" as a substring (they
        //   diverge at the 7th letter — checked directly, not assumed) —
        //   so this card could never auto-complete regardless of actual
        //   response rate. "response rate" appears on that category's
        //   done=true label and doesn't collide with any other category.
        let reviewsDone = checklist.contains { $0.label.localizedCaseInsensitiveContains("google review") && $0.done }
        let responseDone = checklist.contains { $0.label.localizedCaseInsensitiveContains("response rate") && $0.done }
        let gbpDone = (result.gbpScore ?? 0) >= 80
        // social_posts_30d counts PUBLISHED posts only — it used to count
        // pieces drafted through this app, which said "posting consistently"
        // about posts that never went out. 8+ in the trailing 30 days
        // roughly matches this card's own "2–3x per week" claim.
        let socialDone = (result.socialPosts30d ?? 0) >= 8

        let localCards: [RoadmapCard] = [
            RoadmapCard(
                id: "reviews", color: .cavnarAmber,
                title: "Get more Google reviews",
                detail: reviewsDetail(result, done: reviewsDone),
                why: reviewsWhy(result),
                actionLabel: "Send a review request", impact: "Highest impact", done: reviewsDone,
                action: { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "reviews" }
            ),
            RoadmapCard(
                id: "respond", color: .cavnarGreen,
                title: "Respond to every review",
                detail: responseDetail(result, done: responseDone),
                why: responseWhy(result),
                actionLabel: "Go to review queue", impact: "High impact", done: responseDone,
                action: { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "reviews" }
            ),
            RoadmapCard(
                id: "gbp", color: .cavnarBlue,
                title: "Complete your Google Business Profile",
                detail: gbpDetail(result, checklist: checklist),
                why: gbpWhy(result),
                actionLabel: "See what's missing", impact: "Fast win", done: gbpDone,
                action: { withAnimation(.easeOut(duration: 0.2)) { showGbpChecklist = true } }
            ),
            RoadmapCard(
                id: "social", color: .cavnarEmber,
                title: "Post consistently on social",
                detail: socialDetail(result, done: socialDone),
                why: socialWhy(result),
                actionLabel: "Go to marketing", impact: "Long-term", done: socialDone,
                action: { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "marketing" }
            ),
        ]

        // Was always rendered in the same fixed order regardless of which
        // restaurant was looking at it — reviews, respond, gbp, social,
        // every time — which didn't actually match this section's own
        // "RANKED, concrete next steps for YOUR restaurant" promise (see
        // preCheckHero above). A restaurant with strong reviews but a bare
        // GBP profile would still see "Get more Google reviews" listed
        // first even though it's done and irrelevant, with their real
        // biggest gap (GBP) buried third. Not-done items now sort ahead of
        // done ones, and within each group by impact tier — an actual
        // ranking driven by this restaurant's own computed state instead
        // of a static list order.
        // The server's cards when it sends them (already in its order); the
        // local ones only for an older server.
        let serverCards = (result.roadmap ?? []).map { serverCard($0, checklist: checklist) }
        let cards = serverCards.isEmpty ? localCards : serverCards
        let sortedCards = serverCards.isEmpty ? cards.sorted { a, b in
            if a.done != b.done { return !a.done }
            return impactRank(a.impact) < impactRank(b.impact)
        } : serverCards

        let pointsLeft = cards.filter { !$0.done }.count

        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack(alignment: .firstTextBaseline) {
                CavnarKicker("GAPS IN YOUR PUBLIC RECORD")
                Spacer()
                // A count of gaps, not a promise about the score: nothing
                // measures what closing one does to it (NS1 #10).
                CavnarMixedText(pointsLeft > 0 ? "\(pointsLeft) still open" : "None open", role: .secondary)
            }
            VStack(spacing: 0) {
                ForEach(Array(sortedCards.enumerated()), id: \.element.id) { index, card in
                    roadmapRow(card)
                    if index < sortedCards.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                    }
                }
            }
        }
    }

    /// One of the server's roadmap cards, drawn the way the local ones are:
    /// its colour and its button are the phone's (where "Open GBP settings"
    /// goes is a client decision), its words are the server's.
    private func serverCard(_ card: AIVisibilityRoadmapCard, checklist: [AIVisibilityChecklistItem]) -> RoadmapCard {
        let kind = card.key.split(separator: ":").last.map(String.init) ?? card.key
        let color: Color
        let action: () -> Void
        switch kind {
        case "reviews":
            color = .cavnarAmber
            action = { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "reviews" }
        case "responses", "respond":
            color = .cavnarGreen
            action = { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "reviews" }
        case "gbp":
            color = .cavnarBlue
            action = { withAnimation(.easeOut(duration: 0.2)) { showGbpChecklist = true } }
        default:
            color = .cavnarEmber
            action = { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = card.module ?? "marketing" }
        }
        // The GBP card's button opens the checklist on this screen, so its
        // label says that rather than naming a settings page.
        let label = kind == "gbp" ? "See what's missing" : (card.action ?? "Open")
        return RoadmapCard(id: card.key, color: color, title: card.title, detail: card.detail ?? "",
                           why: card.why ?? "", actionLabel: label, impact: card.impact ?? "",
                           done: card.done, action: action,
                           recKey: card.recKey ?? card.key, answerable: card.showsAnswers)
    }

    /// Ordinal for sorting the roadmap by urgency/payoff — matches the
    /// actual impact tiers used across the 4 cards above, not alphabetical.
    private func impactRank(_ impact: String) -> Int {
        switch impact {
        case "Highest impact": return 0
        case "High impact": return 1
        case "Fast win": return 2
        default: return 3
        }
    }

    // MARK: - Per-restaurant roadmap copy
    //
    // Every function below reads directly off this restaurant's own real
    // numbers (review_total/resp_rate/gbp_score/social_posts_30d, all
    // computed server-side from live data — none of it invented client-side)
    // rather than returning a fixed string. "detail" is the always-visible
    // line under each title; "why" is the fuller explanation revealed on
    // tap. Two restaurants in genuinely different situations now read
    // genuinely different guidance instead of the same 4 sentences with a
    // checkmark toggled on or off.

    private func reviewsDetail(_ result: AIVisibilityResult, done: Bool) -> String {
        let total = result.reviewTotal ?? 0
        if done { return "\(total) reviews — past the 50-review AI threshold" }
        let remaining = max(50 - total, 0)
        return "\(total) of 50 reviews — \(remaining) more to go"
    }

    private func reviewsWhy(_ result: AIVisibilityResult) -> String {
        let total = result.reviewTotal ?? 0
        return "You have \(total) review\(total == 1 ? "" : "s") right now. Review count and recency are the most visible public signal about your restaurant, and the one you can move fastest."
    }

    private func responseDetail(_ result: AIVisibilityResult, done: Bool) -> String {
        let rate = Int((result.respRate ?? 0).rounded())
        if done { return "\(rate)% response rate — excellent" }
        let gap = max(75 - rate, 0)
        return "\(rate)% response rate — \(gap)% more gets you to 75%"
    }

    private func responseWhy(_ result: AIVisibilityResult) -> String {
        let rate = Int((result.respRate ?? 0).rounded())
        return "You're currently responding to \(rate)% of your reviews. Replies are published on your public listing, so a guest reading it sees an owner who answers."
    }

    private func gbpDetail(_ result: AIVisibilityResult, checklist: [AIVisibilityChecklistItem]) -> String {
        let score = result.gbpScore ?? 0
        let missingCount = checklist.filter { !$0.done }.count
        guard missingCount > 0 else { return "\(score)% complete" }
        return "\(score)% complete — \(missingCount) item\(missingCount == 1 ? "" : "s") left"
    }

    private func gbpWhy(_ result: AIVisibilityResult) -> String {
        // Was "Your Google Business Profile is X% complete", naming three
        // AI platforms this module never queries and asserting how each one
        // sources its answers. It also read gbpScore, which after the
        // presence/setup split covers review volume and response rate as
        // well as the GBP fields — so the label was wrong twice over.
        "Your public listing and review record score \(result.presenceScore ?? result.gbpScore ?? 0)%. This covers what someone finds when they look you up: your description, hours, phone, website, and how many recent reviews you have."
    }

    private func socialDetail(_ result: AIVisibilityResult, done: Bool) -> String {
        let posts = result.socialPosts30d ?? 0
        if done { return "\(posts) posts published this month — great pace" }
        if posts == 0 { return "No posts published this month yet" }
        return "\(posts) post\(posts == 1 ? "" : "s") published this month — aim for 8+"
    }

    private func socialWhy(_ result: AIVisibilityResult) -> String {
        let posts = result.socialPosts30d ?? 0
        return "You've published \(posts) post\(posts == 1 ? "" : "s") this month. Posts that name your restaurant, neighbourhood and cuisine give search engines more text about you to index."
    }

    /// Was a fully bordered/backgrounded box per card, each with its own
    /// icon badge — the same "container everywhere" problem the rest of
    /// this screen had. Now an accent-bar row with a hairline divider,
    /// matching Competitors' own competitorRow construction directly.
    private func roadmapRow(_ card: RoadmapCard) -> some View {
        let isExpanded = expandedWhy.contains(card.id)
        return HStack(alignment: .top, spacing: CavnarSpace.s) {
            Rectangle().fill(card.done ? Color.cavnarPaper3 : card.color).frame(width: 2.5)
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    // A done gap reads quieter in Ink2 — it used to dim the
                    // whole row to 55%, words included.
                    Text(card.title).cavnarText(.label, color: card.done ? .cavnarInk2 : .cavnarInk)
                    if card.done {
                        Label("Done", systemImage: "checkmark").cavnarText(.secondary, color: .cavnarGreen)
                    } else {
                        // The impact tiers are fixed labels with no
                        // measurement behind them, so the row says only
                        // that the gap is open; the tier still orders it.
                        Text("Open").cavnarText(.tag, color: card.color)
                    }
                }
                Text(card.detail).cavnarText(.secondary).fixedSize(horizontal: false, vertical: true)
                if isExpanded {
                    // The surrounding VStack's own spacing (6) is shared
                    // uniformly by every row here — title, detail, why,
                    // actions — so dropping the why text straight into it
                    // squeezed it to that same tight 6pt on both sides as
                    // everything else, which is what read as crammed.
                    // Extra padding here (only on this element) gives it
                    // real breathing room without loosening the rest of
                    // the card's normally-tighter rhythm.
                    Text(card.why)
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6)
                        .padding(.bottom, 8)
                }
                HStack {
                    if !card.done {
                        Button(action: card.action) {
                            HStack(spacing: 5) {
                                Text(card.actionLabel)
                                // Same external-link arrow the "doing
                                // well" heading uses (marketSection) —
                                // these buttons route the owner
                                // somewhere else in the app, same as
                                // that heading's own "things trending
                                // outward" meaning.
                                Image(systemName: "arrow.up.right")
                                    .font(.cavnar(.caption))
                            }
                        }
                        .buttonStyle(CavnarChipButtonStyle(tone: card.color))
                    }
                    Spacer()
                    Button {
                        // Same fix as the "show more reviews" animation
                        // glitch — .animation(nil, value:) alone wasn't
                        // enough to stop the "why" text from visibly
                        // dropping/fading in as it appears (ScrollView's
                        // own implicit content-resize animation leaking
                        // in); disablesAnimations is the actual override.
                        var transaction = Transaction(animation: nil)
                        transaction.disablesAnimations = true
                        withTransaction(transaction) {
                            if isExpanded { expandedWhy.remove(card.id) } else { expandedWhy.insert(card.id) }
                        }
                        // Opening a keyed card's reasoning is evidence
                        // viewed (#38) — once per card per launch.
                        if !isExpanded, card.recKey != nil {
                            RecEvidenceLog.viewed(key: card.recKey, surface: "intel", module: "intel")
                        }
                    } label: {
                        HStack(spacing: 3) {
                            Text("Why this matters")
                            Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                                .font(.cavnar(.caption))
                                .accessibilityHidden(true)
                        }
                        .cavnarText(.label, color: .cavnarInk2)
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                // The server's open cards are recommendations like any
                // other: Done / Not for us (Intel has nothing to Track).
                if card.answerable, let key = card.recKey {
                    RecAnswerRow(key: key, surface: "intel", module: "intel")
                }
            }
            .animation(nil, value: isExpanded)
        }
        .padding(.vertical, CavnarSpace.s)
    }
}

/// One question the check asked: the question, the first line of the
/// answer, and — tapped — the whole answer in place (iOS readability round,
/// 10/8/26). It used to show the full answer only while a finger was held
/// on the row, in a card floating over the rows below it.
private struct QueryResultRow: View {
    let q: AIVisibilityQuery
    /// This question across the recent checks (the web's dot strip).
    var history: AIVisibilityQueryHistory.Question? = nil
    var runs: [AIVisibilityQueryHistory.Run] = []

    @State private var isExpanded = false

    /// "AI read: tripadvisor.com, opentable.com", each site a link to the
    /// page the answer read.
    private func aiReadLinks(_ links: [(domain: String, url: URL)]) -> AttributedString {
        var out = AttributedString("AI read: ")
        for (i, l) in links.enumerated() {
            var run = AttributedString(l.domain)
            run.link = l.url
            run.underlineStyle = .single
            out += run
            if i < links.count - 1 { out += AttributedString(", ") }
        }
        return out
    }

    /// "On Google: about #4 · shown in 3,796 searches in 28 days". Position is
    /// Search Console's average, so it reads as "about".
    private func googleLine(_ s: AIVisibilitySearch) -> Text {
        var t = Text("On Google: ")
        if let p = s.position {
            t = t + Text("about ") + Text("#\(max(1, Int(p.rounded())))").font(.cavnarNumber(CavnarType.caption, weight: 600))
                + Text(" \u{00B7} ")
        }
        return t + Text("shown in ") + Text((s.impressions ?? 0).formatted()).font(.cavnarNumber(CavnarType.caption, weight: 600))
            + Text(" searches in 28 days")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(alignment: .top, spacing: CavnarSpace.s) {
                    VStack(alignment: .leading, spacing: 2) {
                        if q.search != nil {
                            CavnarKicker("From your Google searches")
                        }
                        Text("\u{201C}\(q.query)\u{201D}")
                            .cavnarText(.label)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(q.answer)
                            .cavnarText(.secondary)
                            .lineLimit(isExpanded ? nil : 1)
                            .truncationMode(.tail)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: CavnarSpace.xs)
                    VStack(alignment: .trailing, spacing: CavnarSpace.xxs) {
                        badge
                        Image(systemName: "chevron.down")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarInk2)
                            .rotationEffect(.degrees(isExpanded ? 180 : 0))
                            .accessibilityHidden(true)
                    }
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint(isExpanded ? "Hides the answer" : "Shows the whole answer")
            if isExpanded {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    if let search = q.search {
                        googleLine(search)
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !q.appeared, !q.sourceLinks.isEmpty {
                        Text(aiReadLinks(Array(q.sourceLinks.prefix(4))))
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarInk2)
                            .tint(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    // What the answer was grounded in, and which of your
                    // competitors it named.
                    if let n = q.sources?.count, n > 0 {
                        Label("\(n) source\(n == 1 ? "" : "s")", systemImage: "link")
                            .cavnarText(.caption)
                    }
                    if let comps = q.competitorsNamed, !comps.isEmpty {
                        Text((q.appeared ? "Also named " : "AI named instead: ") + comps.prefix(3).joined(separator: ", ")
                             + (comps.count > 3 ? " +\(comps.count - 3)" : ""))
                            .cavnarText(.caption, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if q.kind == "branded" {
                        Text("Asked about you by name").cavnarText(.caption)
                    }
                    if let history, history.asked > 0 {
                        historyStrip(history)
                            .padding(.top, 3)
                    }
                }
                .transition(.opacity)
            }
        }
        .padding(.vertical, CavnarSpace.xs)
    }

    /// A dot per check, oldest first: green where the answer named you,
    /// a ring where it didn't, faint where that check didn't ask it — then
    /// "named in 3 of 5 checks" (the web's in2-qc strip, parity #75).
    private func historyStrip(_ h: AIVisibilityQueryHistory.Question) -> some View {
        HStack(spacing: 8) {
            HStack(spacing: 3) {
                ForEach(Array(h.appeared.enumerated()), id: \.offset) { _, a in
                    Circle()
                        .fill(a == true ? Color.cavnarGreen : (a == false ? Color.clear : Color.cavnarPaper3.opacity(0.5)))
                        .overlay(Circle().strokeBorder(a == false ? Color.cavnarInk3 : Color.clear, lineWidth: 1))
                        .frame(width: 7, height: 7)
                }
            }
            HomeMixedText.make(AIVisibilityQueryHistory.line(h), role: .caption)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(historySpoken(h))
    }

    private func historySpoken(_ h: AIVisibilityQueryHistory.Question) -> String {
        var parts = [AIVisibilityQueryHistory.line(h).prefix(1).uppercased() + AIVisibilityQueryHistory.line(h).dropFirst()]
        if let last = runs.last?.at, !last.isEmpty { parts.append("latest check \(CavnarDate.mdyLocal(last))") }
        return parts.joined(separator: ", ")
    }

    private var badge: some View {
        Text(q.appeared ? "Appeared" : "Missed")
            .cavnarText(.tag, color: q.appeared ? .cavnarGreen : .cavnarInk2)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(q.appeared ? Color.cavnarGreenBg : Color.cavnarPaper2)
            .overlay(Capsule().strokeBorder(q.appeared ? Color.clear : Color.cavnarPaper3, lineWidth: 1))
            .clipShape(Capsule())
    }
}
