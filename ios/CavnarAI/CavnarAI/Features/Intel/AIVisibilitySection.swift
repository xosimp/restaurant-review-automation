import SwiftUI

/// One roadmap action card — copy matches the web dashboard's own
/// aiv-roadmap-cards array exactly (renderAIVisibility in dashboard.html),
/// so the guidance an owner reads is identical on both platforms; only the
/// destinations differ where iOS's navigation shape doesn't match web's
/// scroll-to-element behavior (see actions below).
private struct RoadmapCard: Identifiable {
    let id: String
    let color: Color
    let title: String
    let why: String
    let actionLabel: String
    let impact: String
    let timeframe: String
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
                        queriesSection(queries)
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
            Text(q.appeared ? "Appeared" : "Missed")
                .font(.cavnarBody(9.5, weight: 700))
                .foregroundStyle(q.appeared ? Color.cavnarGreen : Color.cavnarInk3)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(q.appeared ? Color.cavnarGreenBg : Color.cavnarPaper2)
                .overlay(Capsule().strokeBorder(q.appeared ? Color.clear : Color.cavnarPaper3, lineWidth: 1))
                .clipShape(Capsule())
        }
        .padding(.vertical, 11)
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

        let cards: [RoadmapCard] = [
            RoadmapCard(
                id: "reviews", color: .cavnarAmber,
                title: "Get more Google reviews",
                why: "AI search tools rank restaurants by review volume and recency. The more reviews you have, the more AI systems trust you.",
                actionLabel: "Send a review request", impact: "Highest impact", timeframe: "Ongoing", done: reviewsDone,
                action: { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "reviews" }
            ),
            RoadmapCard(
                id: "respond", color: .cavnarGreen,
                title: "Respond to every review",
                why: "Response rate signals to Google that your listing is actively managed. Active listings rank higher in local search and are more likely to be cited by AI tools that pull from Google data.",
                actionLabel: "Go to review queue", impact: "High impact", timeframe: "24–48 hrs each", done: responseDone,
                action: { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "reviews" }
            ),
            RoadmapCard(
                id: "gbp", color: .cavnarBlue,
                title: "Complete your Google Business Profile",
                why: "Perplexity, ChatGPT, and Google AI all pull directly from GBP data — hours, photos, menu, description. A complete profile is the single fastest way to become indexable by AI search.",
                actionLabel: "See what's missing", impact: "Fast win", timeframe: "15 min one-time", done: gbpDone,
                action: { withAnimation(.easeOut(duration: 0.2)) { showGbpChecklist = true } }
            ),
            RoadmapCard(
                id: "social", color: .cavnarEmber,
                title: "Post consistently on social",
                why: "Food blogs and social content get indexed by search engines, which AI tools then pull from. Regular posts with your restaurant name, neighborhood, and cuisine type build the online footprint AI needs to find you.",
                actionLabel: "Go to marketing", impact: "Long-term", timeframe: "2–3x per week", done: false,
                action: { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "marketing" }
            ),
        ]

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
                ForEach(Array(cards.enumerated()), id: \.element.id) { index, card in
                    roadmapRow(card)
                    if index < cards.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                    }
                }
            }
        }
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
                Text(card.timeframe).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                if isExpanded {
                    Text(card.why).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk2).lineSpacing(3)
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
