import SwiftUI

/// One roadmap action card — copy matches the web dashboard's own
/// aiv-roadmap-cards array exactly (renderAIVisibility in dashboard.html),
/// so the guidance an owner reads is identical on both platforms; only the
/// destinations differ where iOS's navigation shape doesn't match web's
/// scroll-to-element behavior (see actions below).
private struct RoadmapCard: Identifiable {
    let id: String
    let icon: String
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
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    @State private var showGbpChecklist = false
    @State private var expandedWhy: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if viewModel.result == nil {
                preCheckHero
            }

            checkButton

            if let result = viewModel.result {
                if !result.ok {
                    Text(result.error ?? "Couldn't check AI visibility.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                } else {
                    scoreCard(result)
                    if showGbpChecklist, let checklist = result.checklist {
                        gbpChecklistCard(checklist)
                    }
                    if (result.aiScore ?? 0) == 0 {
                        disclaimerCard
                    }
                    if let queries = result.queries {
                        queriesCard(queries)
                    }
                    if let checklist = result.checklist {
                        roadmapSection(result, checklist: checklist)
                    }
                }
            }
        }
    }

    private var checkButton: some View {
        Button {
            Task { await viewModel.check() }
        } label: {
            if viewModel.isChecking {
                PulsingText("Checking…").frame(maxWidth: .infinity)
            } else {
                Text(viewModel.result == nil ? "Check my AI visibility" : "Re-run")
                    .frame(maxWidth: .infinity)
            }
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
        .disabled(viewModel.isChecking)
    }

    // MARK: - Pre-check hero — this screen used to be one bare button
    // floating over a black void until the first check ran. Explains what
    // the check actually does and previews the three things it returns,
    // so there's something to read/anticipate before tapping, not just an
    // unexplained button with no context for what it's about to do.

    private var preCheckHero: some View {
        VStack(alignment: .leading, spacing: 26) {
            VStack(alignment: .leading, spacing: 10) {
                ZStack {
                    Circle().fill(Color.cavnarEmber.opacity(0.16)).frame(width: 52, height: 52)
                    Image(systemName: "sparkles")
                        .font(.system(size: 20, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber)
                }
                Text("Is your restaurant visible to AI search?")
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

    // MARK: - Score card

    private func scoreCard(_ result: AIVisibilityResult) -> some View {
        HStack(spacing: 10) {
            VStack(spacing: 4) {
                Text("\(result.aiScore ?? 0)%")
                    .font(.cavnarNumber(26, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
                    .cavnarNumberGlow()
                Text("AI APPEARANCE RATE")
                    .font(.cavnarBody(9, weight: 700))
                    .tracking(0.6)
                    .foregroundStyle(Color.cavnarInk3)
                Text(aiScoreLabel(result.aiScore ?? 0))
                    .font(.cavnarBody(10))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .background(Color.cavnarEmber.opacity(0.05))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).strokeBorder(Color.cavnarEmber.opacity(0.18), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))

            Button {
                withAnimation(.easeOut(duration: 0.2)) { showGbpChecklist.toggle() }
            } label: {
                VStack(spacing: 4) {
                    Text("\(result.gbpScore ?? 0)%")
                        .font(.cavnarNumber(26, weight: 700))
                        .foregroundStyle(gbpTone(result.gbpScore ?? 0))
                        .cavnarNumberGlow()
                    Text("GBP COMPLETENESS")
                        .font(.cavnarBody(9, weight: 700))
                        .tracking(0.6)
                        .foregroundStyle(Color.cavnarInk3)
                    Text(gbpScoreLabel(result.gbpScore ?? 0))
                        .font(.cavnarBody(10))
                        .foregroundStyle(Color.cavnarInk3)
                    Text(showGbpChecklist ? "hide breakdown" : "tap to see breakdown")
                        .font(.cavnarBody(9, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background(gbpTone(result.gbpScore ?? 0).opacity(0.08))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).strokeBorder(gbpTone(result.gbpScore ?? 0).opacity(0.25), lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            }
            .buttonStyle(.plain)
        }
    }

    private func aiScoreLabel(_ score: Int) -> String {
        if score >= 67 { return "Strong AI presence" }
        if score >= 34 { return "Moderate presence" }
        return "Not yet indexed by AI search"
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

    private var disclaimerCard: some View {
        Text("Most independent restaurants aren't indexed by AI search yet — this is normal and expected. More reviews, a complete GBP profile, and online mentions are what get you there. That's exactly what this platform drives.")
            .font(.cavnarBody(12))
            .foregroundStyle(Color.cavnarAmber)
            .padding(12)
            .background(Color.cavnarAmberBg)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    // MARK: - GBP checklist breakdown (locked items get a lock icon, not a checkmark)

    private func gbpChecklistCard(_ checklist: [AIVisibilityChecklistItem]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("GBP COMPLETENESS BREAKDOWN")
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            ForEach(checklist) { item in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: item.done ? "checkmark.circle.fill" : (item.needsGmb ? "lock.fill" : "xmark.circle"))
                        .font(.system(size: 13))
                        .foregroundStyle(item.done ? Color.cavnarGreen : (item.needsGmb ? Color.cavnarInk3 : Color.cavnarRed))
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(item.label)
                                .font(.cavnarBody(12, weight: item.done ? 500 : 600))
                                .foregroundStyle(item.done ? Color.cavnarInk3 : Color.cavnarInk)
                            Text("+\(item.pts) pts")
                                .font(.cavnarBody(9, weight: 700))
                                .foregroundStyle(item.done ? Color.cavnarGreen : Color.cavnarInk3)
                        }
                        if !item.done {
                            Text(item.needsGmb ? item.action + " (needs GBP connect)" : item.action)
                                .font(.cavnarBody(11))
                                .foregroundStyle(item.needsGmb ? Color.cavnarInk3 : Color.cavnarEmber)
                        }
                    }
                }
                .padding(.vertical, 3)
            }
        }
        .cavnarCard()
    }

    // MARK: - AI query results

    private func queriesCard(_ queries: [AIVisibilityQuery]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("AI QUERY RESULTS")
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            ForEach(queries) { q in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(q.query).font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Spacer()
                        Image(systemName: q.appeared ? "checkmark.circle.fill" : "xmark.circle")
                            .foregroundStyle(q.appeared ? Color.cavnarGreen : Color.cavnarInk3)
                    }
                    Text(q.answer)
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineLimit(3)
                }
                .padding(.vertical, 4)
            }
        }
        .cavnarCard()
    }

    // MARK: - Roadmap

    private func roadmapSection(_ result: AIVisibilityResult, checklist: [AIVisibilityChecklistItem]) -> some View {
        let reviewsDone = checklist.contains { $0.label.localizedCaseInsensitiveContains("review") && $0.done }
        let responseDone = checklist.contains { $0.label.localizedCaseInsensitiveContains("respond") && $0.done }
        let gbpDone = (result.gbpScore ?? 0) >= 80

        let cards: [RoadmapCard] = [
            RoadmapCard(
                id: "reviews", icon: "star.fill", color: .cavnarAmber,
                title: "Get more Google reviews",
                why: "AI search tools rank restaurants by review volume and recency. The more reviews you have, the more AI systems trust you.",
                actionLabel: "Send a review request", impact: "Highest impact", timeframe: "Ongoing", done: reviewsDone,
                action: { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "reviews" }
            ),
            RoadmapCard(
                id: "respond", icon: "bubble.left.fill", color: .cavnarGreen,
                title: "Respond to every review",
                why: "Response rate signals to Google that your listing is actively managed. Active listings rank higher in local search and are more likely to be cited by AI tools that pull from Google data.",
                actionLabel: "Go to review queue", impact: "High impact", timeframe: "24–48 hrs each", done: responseDone,
                action: { deepLinkRouter.pendingTab = .modules; deepLinkRouter.pendingModuleKey = "reviews" }
            ),
            RoadmapCard(
                id: "gbp", icon: "mappin.circle.fill", color: .cavnarBlue,
                title: "Complete your Google Business Profile",
                why: "Perplexity, ChatGPT, and Google AI all pull directly from GBP data — hours, photos, menu, description. A complete profile is the single fastest way to become indexable by AI search.",
                actionLabel: "See what's missing", impact: "Fast win", timeframe: "15 min one-time", done: gbpDone,
                action: { withAnimation(.easeOut(duration: 0.2)) { showGbpChecklist = true } }
            ),
            RoadmapCard(
                id: "social", icon: "bolt.fill", color: .cavnarEmber,
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
            ForEach(cards) { card in
                roadmapCardView(card)
            }
        }
    }

    private func roadmapCardView(_ card: RoadmapCard) -> some View {
        let isExpanded = expandedWhy.contains(card.id)
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 11) {
                ZStack {
                    RoundedRectangle(cornerRadius: 7).fill(card.color.opacity(0.16)).frame(width: 28, height: 28)
                    Image(systemName: card.icon).font(.system(size: 12)).foregroundStyle(card.color)
                }
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        Text(card.title).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                        if card.done {
                            Label("Done", systemImage: "checkmark").font(.cavnarBody(10, weight: 600)).foregroundStyle(Color.cavnarGreen)
                        } else {
                            Text(card.impact.uppercased()).font(.cavnarBody(10, weight: 600)).tracking(0.4).foregroundStyle(card.color)
                        }
                    }
                    Text(card.timeframe).font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                }
            }
            if isExpanded {
                Text(card.why).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk2).lineSpacing(3)
            }
            HStack {
                if !card.done {
                    Button(action: card.action) {
                        Text(card.actionLabel)
                            .font(.cavnarBody(11, weight: 600))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 13)
                            .padding(.vertical, 6)
                            .background(card.color)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                }
                Spacer()
                Button {
                    if isExpanded { expandedWhy.remove(card.id) } else { expandedWhy.insert(card.id) }
                } label: {
                    Text("Why this matters")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                        .underline()
                }
            }
        }
        .padding(13)
        .opacity(card.done ? 0.55 : 1)
        .overlay(alignment: .leading) {
            Rectangle().fill(card.color).frame(width: 3)
        }
        .background(Color.cavnarSurface)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}
