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
/// NOTE: dashboard.html's own renderAIVisibility (aiv-roadmap-cards) still
/// shows the old static copy — this personalization is iOS-only so far.
/// An owner comparing web and mobile side by side will see different text
/// for the same restaurant until web gets the same treatment.
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

    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
            if viewModel.result == nil {
                preCheckHero
                checkButton
            } else if let result = viewModel.result {
                if !result.ok {
                    Text(result.error ?? "Couldn't check AI visibility.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                    checkButton
                } else {
                    heroPanel(result)
                    if showGbpChecklist, let checklist = result.checklist {
                        gbpChecklistGrid(checklist)
                    }
                    if let queries = result.queries {
                        // Each row's own .zIndex(isPressed ? 1 : 0) only
                        // controls paint order among ITS siblings inside
                        // queriesSection's own inner VStack — it has no
                        // effect on queriesSection's paint order relative
                        // to roadmapSection, an entirely separate sibling
                        // section right after it in THIS outer VStack.
                        // Only the last query row sits close enough to
                        // queriesSection's own bottom edge that its
                        // popped-up card can overflow into where
                        // roadmapSection begins — and since roadmapSection
                        // is declared after queriesSection with no zIndex
                        // difference, it was painting on top of that
                        // overflow, which is what made the "YOUR AI
                        // VISIBILITY ROADMAP" heading look like it was
                        // sitting inside the popup. queriesSection always
                        // sits above roadmapSection in normal layout, so
                        // this has zero effect on the non-pressed state —
                        // it only matters for exactly this overflow case.
                        queriesSection(queries)
                            .zIndex(1)
                    }
                    if let checklist = result.checklist {
                        roadmapSection(result, checklist: checklist)
                    }
                    checkButton
                }
            }
        }
    }

    private var checkButton: some View {
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
        // The button itself stays hug-content sized — this centers that
        // hug-content button within the full width instead of letting the
        // parent's .leading-aligned VStack pin it to the left edge.
        .frame(maxWidth: .infinity)
    }

    // MARK: - Pre-check hero — this screen used to be one bare button
    // floating over a black void until the first check ran. Explains what
    // the check actually does and previews the three things it returns,
    // so there's something to read/anticipate before tapping, not just an
    // unexplained button with no context for what it's about to do.

    private var preCheckHero: some View {
        VStack(alignment: .leading, spacing: 26) {
            VStack(alignment: .leading, spacing: 10) {
                Text("Is \(restaurantName ?? "your restaurant") visible to AI search?")
                    .font(.cavnarHeadline(21))
                    .foregroundStyle(Color.cavnarInk)
                    .lineSpacing(3)
                Text("More guests are asking ChatGPT, Perplexity, and Google AI where to eat before they ever open Maps. This checks whether you actually show up in those answers — and exactly what to fix if you don't.")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineSpacing(4)
            }

            VStack(alignment: .leading, spacing: 16) {
                previewRow(
                    icon: "text.bubble.fill", tone: Color.cavnarEmber,
                    title: "AI query results",
                    detail: "The exact questions real guests ask AI tools, and whether your restaurant came up."
                )
                previewRow(
                    icon: "checklist", tone: Color.cavnarGreen,
                    title: "GBP completeness score",
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
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 8).fill(tone.opacity(0.16)).frame(width: 32, height: 32)
                Image(systemName: icon).font(.system(size: 13, weight: .semibold)).foregroundStyle(tone)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                Text(detail).font(.cavnarBody(11.5)).foregroundStyle(Color.cavnarInk3).lineSpacing(2)
            }
        }
    }

    // MARK: - Hero — was two separately-boxed score tiles plus a whole
    // extra bordered "disclaimer" card for the 0%-score case. One panel
    // now, mirroring FoodCost's own heroCard gradient/border/glow
    // treatment: both numbers side by side, one computed summary sentence
    // (folds the old disclaimer's reassurance in for the 0%-score case
    // instead of giving it a separate box), and the GBP number stays the
    // one interactive element — tap it to reveal the breakdown grid below.

    private func heroPanel(_ result: AIVisibilityResult) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 0) {
                heroStat(
                    value: "\(result.aiScore ?? 0)%", tone: aiScoreTone(result.aiScore ?? 0),
                    label: "AI APPEARANCE", sub: aiScoreLabel(result.aiScore ?? 0)
                )
                Rectangle().fill(Color.cavnarEmber.opacity(0.3)).frame(width: 1).padding(.vertical, 6)
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.2)) { showGbpChecklist.toggle() }
                } label: {
                    heroStat(
                        value: "\(result.gbpScore ?? 0)%", tone: gbpTone(result.gbpScore ?? 0),
                        label: "GBP COMPLETE", sub: gbpScoreLabel(result.gbpScore ?? 0),
                        expandable: true, isExpanded: showGbpChecklist
                    )
                }
                .buttonStyle(.plain)
            }
            if let insight = heroInsightText(result) {
                Rectangle().fill(Color.cavnarEmber.opacity(0.25)).frame(height: 1).padding(.top, 16)
                insight
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineSpacing(3)
                    .padding(.top, 12)
            }
        }
        .padding(18)
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

    /// value arrives pre-styled by the caller (glow tint differs per stat) —
    /// mirrors statTile's own doc comment in IntelView for why this never
    /// applies its own color on top of what's passed in.
    private func heroStat(value: String, tone: Color, label: String, sub: String, expandable: Bool = false, isExpanded: Bool = false) -> some View {
        VStack(spacing: 4) {
            Text(value)
                .font(.cavnarNumber(28, weight: 700))
                .foregroundStyle(tone)
                .cavnarNumberGlow(tone)
            HStack(spacing: 3) {
                Text(label)
                    .font(.cavnarBody(9, weight: 700))
                    .tracking(0.6)
                    .foregroundStyle(Color.cavnarInk.opacity(0.55))
                if expandable {
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.system(size: 7, weight: .bold))
                        .foregroundStyle(Color.cavnarInk.opacity(0.5))
                }
            }
            Text(sub)
                .font(.cavnarBody(10))
                .foregroundStyle(Color.cavnarInk.opacity(0.55))
        }
        .frame(maxWidth: .infinity)
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
        let appeared = result.appearedCount ?? 0
        let total = result.totalQueries ?? 0
        guard total > 0 else { return nil }
        if appeared == 0 {
            return Text("Not yet appearing in AI search — normal for independent restaurants this early. More reviews and a complete Google Business Profile are what get you there.")
        }
        var text = Text("Appears in ")
            + highlightedNumber(appeared)
            + Text(" of ")
            + highlightedNumber(total)
            + Text(" AI search queries.")
        if let topGap = (result.checklist ?? []).filter({ !$0.done }).max(by: { $0.pts < $1.pts }) {
            text = text + Text(" \(topGap.action) is the fastest way to close the gap.")
        }
        return text
    }

    private func highlightedNumber(_ value: Int) -> Text {
        Text("\(value)")
            .font(.cavnarNumber(16, weight: 700))
            .foregroundStyle(Color.cavnarEmber)
    }

    private func aiScoreLabel(_ score: Int) -> String {
        if score >= 67 { return "Strong AI presence" }
        if score >= 34 { return "Moderate presence" }
        return "Not yet indexed by AI search"
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

    private func gbpTone(_ score: Int) -> Color {
        if score >= 70 { return .cavnarGreen }
        if score >= 40 { return .cavnarAmber }
        return .cavnarRed
    }

    // MARK: - GBP checklist breakdown — was a full-width, single-column
    // bordered card with one row per item; a 2-column grid gets the same
    // six items into roughly half the vertical space, unboxed to match
    // every other section on this screen now.

    private func gbpChecklistGrid(_ checklist: [AIVisibilityChecklistItem]) -> some View {
        let doneCount = checklist.filter(\.done).count
        return VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("GBP COMPLETENESS")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                Spacer()
                Text("\(doneCount)/\(checklist.count) done")
                    .font(.cavnarBody(10, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
            }
            LazyVGrid(columns: [GridItem(.flexible(), alignment: .top), GridItem(.flexible(), alignment: .top)], alignment: .leading, spacing: 0) {
                ForEach(checklist) { item in
                    gbpGridItem(item)
                }
            }
        }
    }

    private func gbpGridItem(_ item: AIVisibilityChecklistItem) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: item.done ? "checkmark.circle.fill" : (item.needsGmb ? "lock.fill" : "circle"))
                .font(.system(size: 11))
                .foregroundStyle(item.done ? Color.cavnarGreen : Color.cavnarInk3)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text(item.label)
                        .font(.cavnarBody(11.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .lineLimit(1)
                    Text("+\(item.pts)")
                        .font(.cavnarBody(9))
                        .foregroundStyle(Color.cavnarInk3)
                }
                if !item.done {
                    Text(item.needsGmb ? item.action + " (needs GBP)" : item.action)
                        .font(.cavnarBody(10))
                        .foregroundStyle(Color.cavnarEmber)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 9)
        .padding(.trailing, 6)
        .opacity(item.done ? 0.5 : 1)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Color.cavnarPaper3).frame(height: 1)
        }
    }

    // MARK: - AI query results — was a full paragraph answer under every
    // query in its own bordered card; a single-line truncated answer plus
    // a status chip carries the same "did we appear" signal at a glance.

    private func queriesSection(_ queries: [AIVisibilityQuery]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("AI QUERY RESULTS")
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) {
                ForEach(Array(queries.enumerated()), id: \.element.id) { index, q in
                    queryRow(q)
                    if index < queries.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                    }
                }
            }
        }
    }

    private func queryRow(_ q: AIVisibilityQuery) -> some View {
        QueryResultRow(q: q)
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
        // marketing_content_log only proves content was drafted through
        // this app, not confirmed-posted to a platform — an imperfect
        // signal, but a real, live one. 8+ in the trailing 30 days roughly
        // matches this card's own "2–3x per week" claim.
        let socialDone = (result.socialPosts30d ?? 0) >= 8

        let cards: [RoadmapCard] = [
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
        let sortedCards = cards.sorted { a, b in
            if a.done != b.done { return !a.done }
            return impactRank(a.impact) < impactRank(b.impact)
        }

        let pointsLeft = cards.filter { !$0.done }.count

        return VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("YOUR AI VISIBILITY ROADMAP")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                Spacer()
                Text(pointsLeft > 0 ? "\(pointsLeft) action\(pointsLeft == 1 ? "" : "s") to grow your score" : "All steps complete")
                    .font(.cavnarBody(10, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
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
        return "You have \(total) review\(total == 1 ? "" : "s") right now. AI search tools rank restaurants by review volume and recency — the more you have, the more AI systems trust you."
    }

    private func responseDetail(_ result: AIVisibilityResult, done: Bool) -> String {
        let rate = Int((result.respRate ?? 0).rounded())
        if done { return "\(rate)% response rate — excellent" }
        let gap = max(75 - rate, 0)
        return "\(rate)% response rate — \(gap)% more gets you to 75%"
    }

    private func responseWhy(_ result: AIVisibilityResult) -> String {
        let rate = Int((result.respRate ?? 0).rounded())
        return "You're currently responding to \(rate)% of your reviews. Response rate signals to Google that your listing is actively managed — active listings rank higher in local search and are more likely to be cited by AI tools that pull from Google data."
    }

    private func gbpDetail(_ result: AIVisibilityResult, checklist: [AIVisibilityChecklistItem]) -> String {
        let score = result.gbpScore ?? 0
        let missingCount = checklist.filter { !$0.done }.count
        guard missingCount > 0 else { return "\(score)% complete" }
        return "\(score)% complete — \(missingCount) item\(missingCount == 1 ? "" : "s") left"
    }

    private func gbpWhy(_ result: AIVisibilityResult) -> String {
        "Your Google Business Profile is \(result.gbpScore ?? 0)% complete. Perplexity, ChatGPT, and Google AI all pull directly from GBP data — hours, photos, menu, description. A complete profile is the single fastest way to become indexable by AI search."
    }

    private func socialDetail(_ result: AIVisibilityResult, done: Bool) -> String {
        let posts = result.socialPosts30d ?? 0
        if done { return "\(posts) posts this month — great pace" }
        if posts == 0 { return "No marketing pieces logged this month yet" }
        return "\(posts) post\(posts == 1 ? "" : "s") this month — aim for 8+"
    }

    private func socialWhy(_ result: AIVisibilityResult) -> String {
        let posts = result.socialPosts30d ?? 0
        return "You've logged \(posts) marketing piece\(posts == 1 ? "" : "s") this month. Food blogs and social content get indexed by search engines, which AI tools then pull from — regular posts with your restaurant name, neighborhood, and cuisine type build the online footprint AI needs to find you."
    }

    /// Was a fully bordered/backgrounded box per card, each with its own
    /// icon badge — the same "container everywhere" problem the rest of
    /// this screen had. Now an accent-bar row with a hairline divider,
    /// matching Competitors' own competitorRow construction directly.
    private func roadmapRow(_ card: RoadmapCard) -> some View {
        let isExpanded = expandedWhy.contains(card.id)
        return HStack(alignment: .top, spacing: 12) {
            Rectangle().fill(card.color).frame(width: 2.5)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Text(card.title).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                    if card.done {
                        Label("Done", systemImage: "checkmark").font(.cavnarBody(10, weight: 600)).foregroundStyle(Color.cavnarGreen)
                    } else {
                        Text(card.impact.uppercased()).font(.cavnarBody(10, weight: 600)).tracking(0.4).foregroundStyle(card.color)
                    }
                }
                Text(card.detail).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
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
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarInk2)
                        .lineSpacing(5)
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
                                    .font(.system(size: 9, weight: .bold))
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
                    } label: {
                        HStack(spacing: 3) {
                            Text("Why this matters")
                            Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                                .font(.system(size: 8, weight: .bold))
                        }
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                    }
                }
            }
            .animation(nil, value: isExpanded)
        }
        .padding(.vertical, 14)
        .opacity(card.done ? 0.55 : 1)
    }
}

/// A query result's answer is truncated to one line by default — most of
/// it gets cut off. Press and hold to read the full thing; release to
/// collapse back. @GestureState (not plain @State) is what gives the
/// "release collapses it" behavior for free: it automatically resets to
/// its initial value the instant the gesture ends, whether that's a
/// clean lift, a cancel, or the view disappearing — no separate onEnded
/// handler needed to remember to reset anything. Needs its own View
/// struct (not a plain function like the rest of this file's rows) since
/// @GestureState requires real per-instance storage, one independent
/// press-state per row rather than one shared across all of them.
private struct QueryResultRow: View {
    let q: AIVisibilityQuery

    @GestureState private var isPressed = false

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text("\u{201C}\(q.query)\u{201D}")
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text(q.answer)
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 8)
            badge
        }
        .padding(.vertical, 11)
        // .overlay (not inline lineLimit(nil)) — the base row's own
        // reported height never changes with press state, so nothing
        // below it in the list shifts. The full-text card floats above
        // it instead, matching how Apple's own long-press previews work
        // (Messages, Mail): the row underneath doesn't grow, a separate
        // elevated surface appears over it.
        .overlay {
            if isPressed {
                expandedCard
                    .transition(.scale(scale: 0.92, anchor: .center).combined(with: .opacity))
            }
        }
        // Paints above the next row's own divider/content once its card
        // is taller than one line — zIndex has to live on this row itself
        // (not just inside expandedCard) since paint order between
        // sibling rows is decided at the outer ForEach's level, not
        // inside any one row's own subtree.
        .zIndex(isPressed ? 1 : 0)
        .animation(.spring(response: 0.32, dampingFraction: 0.75), value: isPressed)
        .contentShape(Rectangle())
        // LongPressGesture, not DragGesture(minimumDistance: 0) — that
        // was the ORIGINAL "too sensitive, fires on scroll" bug. A zero-
        // distance DragGesture recognizes the instant a finger touches
        // down, indistinguishable from the very start of a scroll.
        // LongPressGesture requires the touch to stay put for
        // minimumDuration before it succeeds at all (0.5s — the same
        // default UIKit's own UILongPressGestureRecognizer uses); a
        // scroll's own movement makes the gesture fail to recognize
        // rather than firing early.
        //
        // But a BARE LongPressGesture + @GestureState turned out to have
        // its own separate, independently-documented problem (confirmed
        // against multiple reports, not guessed): it only reports its
        // value ONCE, at the instant it succeeds — it never reports again
        // while the finger stays down. With nothing else arriving,
        // @GestureState reads that silence as "the gesture must be over"
        // and resets to false almost immediately, which is exactly why
        // the card was flashing and collapsing instead of staying up
        // for the hold. Sequencing a zero-distance DragGesture AFTER the
        // long press (Apple's own documented composition for this) fixes
        // it: once the long press succeeds, that drag keeps reporting
        // continuously for as long as the touch is down (even with zero
        // movement), which is what keeps .second(true, _) — and so
        // isPressed — genuinely true for the whole hold, only resetting
        // when the finger actually lifts.
        .gesture(
            // 0.3s, not the 0.5s UIKit default — that reads as stiff for a
            // quick "peek at the answer" interaction specifically (as
            // opposed to something heavier like drag-to-reorder, which is
            // what the 0.5s default is really tuned for). This doesn't
            // reopen the original scroll-sensitivity bug: what actually
            // disambiguates a hold from a scroll is LongPressGesture's own
            // movement-tolerance check (it fails to recognize once the
            // touch moves past a small threshold), not the duration — a
            // real scroll starts moving immediately regardless of how
            // short this number is.
            LongPressGesture(minimumDuration: 0.3)
                .sequenced(before: DragGesture(minimumDistance: 0))
                .updating($isPressed) { value, state, _ in
                    switch value {
                    case .second(true, _):
                        if !state { Haptic.light() }
                        state = true
                    default:
                        state = false
                    }
                }
        )
    }

    private var badge: some View {
        Text(q.appeared ? "Appeared" : "Missed")
            .font(.cavnarBody(9.5, weight: 700))
            .foregroundStyle(q.appeared ? Color.cavnarGreen : Color.cavnarInk3)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(q.appeared ? Color.cavnarGreenBg : Color.cavnarPaper2)
            .overlay(Capsule().strokeBorder(q.appeared ? Color.clear : Color.cavnarPaper3, lineWidth: 1))
            .clipShape(Capsule())
    }

    // Bigger shadow + a slight scale-up is the standard "lifted off the
    // surface toward the viewer" cue — the same visual language Apple's
    // own long-press previews use — rather than a flat in-place cross-fade.
    private var expandedCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 8) {
                Text("\u{201C}\(q.query)\u{201D}")
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 8)
                badge
            }
            Text(q.answer)
                .font(.cavnarBody(11.5))
                .foregroundStyle(Color.cavnarInk2)
                .lineSpacing(3)
                // .overlay proposes this card the BASE row's own compact,
                // single-line height — without this, a VStack asked to fit
                // into a proposal smaller than it needs will compress its
                // flexible children to match rather than grow past it, so
                // this Text was silently losing lines to that compression
                // (not to any lineLimit, which was never set) instead of
                // reporting its own true multi-line height. This forces it
                // to keep its real ideal height regardless of what's proposed.
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        // Same reasoning one level up — the whole card needs to report ITS
        // real ideal size too, not just the Text inside it, or the outer
        // VStack this is an overlay on top of would still cap it.
        .fixedSize(horizontal: false, vertical: true)
        .background(Color.cavnarPaper2)
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3.opacity(0.7), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        .shadow(color: .black.opacity(0.5), radius: 20, x: 0, y: 10)
        .scaleEffect(1.04)
    }
}
