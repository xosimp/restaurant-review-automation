import Observation
import SwiftUI

/// The Marketing Opportunity Feed on the phone (parity audit #28): what the
/// data supports doing now, ranked — no model call on load; a draft is made
/// when the owner taps "Draft it". Only the cards on screen are logged as
/// shown: the first `visible`, or every card once the owner asks for the
/// rest (`?show=all`). The web's `#mkt-opps`.
@Observable
@MainActor
final class MarketingOpportunityViewModel {
    var items: [MarketingOpportunity] = []
    var sources: [OpportunityFeed.Source] = []
    var checked: [String] = []
    var visible = 3
    /// "Show more" was pressed: every card is on screen from here.
    private(set) var open = false
    var isLoading = false
    private(set) var loaded = false
    var loadError: String?
    var answerError: String?
    /// A card a link asked for (marketing/opportunities?card=<key>).
    var focusedKey: String?
    private var opened: Set<String> = []
    private var seq = 0

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    var shown: [MarketingOpportunity] { open ? items : Array(items.prefix(visible)) }
    var hiddenCount: Int { open ? 0 : max(0, items.count - visible) }
    var badSources: [String] { sources.filter { $0.state == "failed" }.map(\.label) }
    var emptyLine: (text: String, warn: Bool) { OpportunityFeed.emptyLine(sources: sources, checked: checked) }

    func load(quiet: Bool = false) async {
        seq += 1
        let my = seq
        if !quiet { isLoading = true }
        defer { if my == seq { isLoading = false } }
        do {
            let feed: OpportunityFeed = try await client.send(
                "/mobile/api/marketing/opportunities", query: open ? ["show": "all"] : [:], hapticOnError: false)
            guard my == seq else { return }
            guard feed.ok else {
                items = []
                loadError = feed.error ?? "Couldn\u{2019}t read your opportunities right now."
                return
            }
            apply(feed)
        } catch is CancellationError {
            return
        } catch let error as APIClient.APIError {
            guard my == seq else { return }
            // A failed reload leaves no stale "Show N more".
            items = []
            loadError = error.message
        } catch {
            guard my == seq else { return }
            items = []
            loadError = "Couldn\u{2019}t read your opportunities right now."
        }
    }

    private func apply(_ feed: OpportunityFeed) {
        items = feed.items
        sources = feed.sources
        checked = feed.checked
        visible = max(1, feed.visible)
        loadError = nil
        loaded = true
    }

    /// "Show more": the rest are on screen from here, so the server logs
    /// them as shown now — not on load, when a card behind the fold nobody
    /// saw expired as ignored.
    func showMore() async {
        open = true
        await load(quiet: true)
    }

    /// Brings a linked card on screen (behind "Show more" when it must).
    func focus(_ key: String?) async {
        guard let key, !key.isEmpty else { return }
        focusedKey = key
        if let i = items.firstIndex(where: { $0.key == key }), i >= visible, !open {
            await showMore()
        }
    }

    /// Opened, not answered — once per card per launch of the screen.
    func recordOpened(_ key: String) {
        guard opened.insert(key).inserted else { return }
        let body = APIClient.RecEventBody(key: key, event: "opened", surface: "marketing", module: "marketing", kind: nil)
        Task {
            let _: APIClient.OKResponse? = try? await client.send("/mobile/api/recs/event", method: .post, body: body,
                                                                  hapticOnError: false, retryTransient: false)
        }
    }

    /// Done / Not for us from a swipe: the same one door (POST
    /// /mobile/api/recs/event) RecAnswerRow uses. The card leaves at once
    /// once the server has it.
    func answer(_ card: MarketingOpportunity, _ answer: RecAnswer, reason: RecReason? = nil) async {
        answerError = nil
        do {
            let r = try await client.answerRecommendation(key: card.key, answer: answer, surface: "marketing",
                                                          module: "marketing", reasonCode: reason?.code)
            guard r.ok else {
                answerError = r.error ?? "Couldn\u{2019}t save that."
                return
            }
            Haptic.success()
            await answered(card.key)
        } catch is CancellationError {
            return
        } catch let error as APIClient.APIError {
            answerError = error.message
        } catch {
            answerError = "Couldn\u{2019}t save that."
        }
    }

    /// A card answered here leaves the feed; the list stays as open as it
    /// was. A card that moves up from behind "Show more" was never logged
    /// as shown, so a quiet reload logs it.
    func answered(_ key: String) async {
        let had = items.contains { $0.key == key }
        items.removeAll { $0.key == key }
        guard had, !open, items.count >= visible, items[visible - 1].recId == nil else { return }
        await load(quiet: true)
    }
}

/// The feed on the phone (readability round 10/8/26): ONE card in full —
/// the best opportunity, with its one primary — and every other card the
/// server counts as shown as a one-line row that opens into its card in
/// place, so a card the server logs as shown is always on screen; "Show N
/// more" brings the rest. The facts behind a card wait behind "Details";
/// the swipe hint is said once per device.
struct MarketingOpportunitySection: View {
    let viewModel: MarketingOpportunityViewModel
    /// The card whose "Draft it" is drafting, if any — every other waits.
    var draftingKey: String?
    var onDraft: (MarketingOpportunity) -> Void

    @State private var rowHeights: [String: CGFloat] = [:]
    @State private var reasonFor: MarketingOpportunity?
    /// Cards opened from their one-line row (the first is always open).
    @State private var openKeys: Set<String> = []
    /// Cards whose facts are showing.
    @State private var detailKeys: Set<String> = []
    /// The swipe hint, said once per device (a convenience, so local).
    @AppStorage("cavnar.marketing.swipeHintSeen") private var swipeHintSeen = false
    @State private var showsSwipeHint = false

    private func isOpen(_ card: MarketingOpportunity, index: Int) -> Bool {
        index == 0 || openKeys.contains(card.key) || viewModel.focusedKey == card.key
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                CavnarKicker("Cavnar AI found")
                Text("Opportunities this week").cavnarText(.headline)
            }
            if viewModel.isLoading && viewModel.items.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    CavnarSkeletonBar(height: 3)
                    Text("Looking for this week\u{2019}s opportunities")
                        .cavnarText(.caption)
                }
            } else if let error = viewModel.loadError {
                HStack(spacing: 10) {
                    Text(error).cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer()
                    Button("Retry") { Task { await viewModel.load() } }
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 44)
                }
            } else if viewModel.loaded && viewModel.items.isEmpty {
                let line = viewModel.emptyLine
                CampaignCheckLine(ok: !line.warn, text: line.text)
            } else if !viewModel.items.isEmpty {
                let cards = viewModel.shown
                List {
                    ForEach(Array(cards.enumerated()), id: \.element.id) { i, card in
                        Group {
                            if isOpen(card, index: i) {
                                cardView(card, primary: i == 0)
                            } else {
                                compactRow(card)
                            }
                        }
                            .cavnarReportsRowHeight(card.key, into: $rowHeights)
                            .listRowBackground(Color.clear)
                            .listRowInsets(EdgeInsets(top: 6, leading: 0, bottom: 6, trailing: 0))
                            .listRowSeparator(.hidden)
                            .swipeActions(edge: .leading, allowsFullSwipe: false) {
                                Button {
                                    swipeHintSeen = true
                                    Task { await viewModel.answer(card, .completed) }
                                } label: { Label(RecAnswer.completed.label, systemImage: "checkmark") }
                                .tint(Color.cavnarGreen)
                            }
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                Button {
                                    swipeHintSeen = true
                                    reasonFor = card
                                } label: { Label(RecAnswer.notForUs.label, systemImage: "hand.raised") }
                                .tint(Color.cavnarInk3)
                            }
                            .contextMenu {
                                Button { onDraft(card) } label: { Label("Draft it", systemImage: "sparkles") }
                                Button { Task { await viewModel.answer(card, .completed) } } label: {
                                    Label(RecAnswer.completed.label, systemImage: "checkmark")
                                }
                                Button { reasonFor = card } label: {
                                    Label(RecAnswer.notForUs.label, systemImage: "hand.raised")
                                }
                            }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .scrollDisabled(true)
                .frame(height: CavnarFittedList.height(ids: cards.map(\.key), measured: rowHeights, verticalInsets: 12))
                .recReasonDialog(isPresented: Binding(get: { reasonFor != nil }, set: { if !$0 { reasonFor = nil } }),
                                 skipLabel: "Just hide it",
                                 onSkip: {
                                     if let card = reasonFor { Task { await viewModel.answer(card, .notForUs) } }
                                 }) { reason in
                    if let card = reasonFor { Task { await viewModel.answer(card, .notForUs, reason: reason) } }
                }
                if viewModel.hiddenCount > 0 {
                    Button {
                        Haptic.light()
                        Task { await viewModel.showMore() }
                    } label: {
                        HStack(spacing: CavnarSpace.xxs + 2) {
                            HomeMixedText.make("Show \(viewModel.hiddenCount) more", role: .label,
                                               color: .cavnarEmber2, numberColor: .cavnarEmber2)
                            Image(systemName: "chevron.down")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarEmber2)
                                .accessibilityHidden(true)
                            Spacer(minLength: 0)
                        }
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                if !viewModel.badSources.isEmpty {
                    Text("Couldn\u{2019}t check \(viewModel.badSources.joined(separator: ", ")) just now.")
                        .cavnarText(.caption)
                }
                if showsSwipeHint {
                    Text("Swipe a card right for Done, left for Not for us.")
                        .cavnarText(.caption)
                }
            }
            if let error = viewModel.answerError {
                Text(error).cavnarText(.caption, color: .cavnarRedText)
            }
        }
        .onAppear {
            // Said the first time the feed is seen on this device, then not.
            if !swipeHintSeen {
                showsSwipeHint = true
                swipeHintSeen = true
            }
        }
    }

    /// A card the server counts as shown, folded to one line: its kind and
    /// title; a tap opens the full card in place.
    private func compactRow(_ card: MarketingOpportunity) -> some View {
        Button {
            Haptic.light()
            viewModel.recordOpened(card.key)
            withAnimation(.easeOut(duration: 0.2)) { _ = openKeys.insert(card.key) }
        } label: {
            HStack(alignment: .center, spacing: CavnarSpace.s) {
                VStack(alignment: .leading, spacing: 2) {
                    CavnarKicker(card.kindLabel)
                    CavnarMixedText(card.title, role: .label)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.down")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, CavnarSpace.m)
            .padding(.vertical, CavnarSpace.s)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background(Color.cavnarPaper2)
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens this opportunity")
    }

    private func cardView(_ card: MarketingOpportunity, primary: Bool) -> some View {
        let focused = viewModel.focusedKey == card.key
        let drafting = draftingKey == card.key
        let hasFacts = card.stakeLine != nil || !card.facts.isEmpty
        let showingFacts = detailKeys.contains(card.key)
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: 8) {
                CavnarKicker(card.kindLabel)
                if let when = card.whenLabel {
                    HomeMixedText.make(when, role: .caption)
                }
                if card.rankedByResults {
                    AccountChip(text: "Ranked by your results", muted: true)
                }
                Spacer(minLength: 0)
            }
            CavnarMixedText(card.title, role: .lead)
            if let why = card.why {
                CavnarMixedText(why, role: .secondary)
            }
            // The figures behind it, behind "Details" (readability round).
            if hasFacts {
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.2)) {
                        if showingFacts { detailKeys.remove(card.key) } else { detailKeys.insert(card.key) }
                    }
                } label: {
                    HStack(spacing: CavnarSpace.xxs + 2) {
                        Text(showingFacts ? "Hide details" : "Details")
                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        Image(systemName: "chevron.down")
                            .font(.cavnar(.caption))
                            .rotationEffect(.degrees(showingFacts ? 180 : 0))
                            .accessibilityHidden(true)
                        Spacer(minLength: 0)
                    }
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityValue(showingFacts ? "Expanded" : "Collapsed")
                if showingFacts {
                    AccountFlowLayout(spacing: 6, lineSpacing: 6) {
                        if let stake = card.stakeLine {
                            HomeMixedText.make(stake, role: .caption, color: .cavnarInk, numberColor: .cavnarInk)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(Capsule().fill(Color.cavnarEmber.opacity(0.16)))
                        }
                        ForEach(card.facts, id: \.self) { fact in
                            HomeMixedText.make(fact, role: .caption, color: .cavnarInk2)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(Capsule().fill(Color.white.opacity(0.05)))
                        }
                    }
                }
            }
            if let conflict = card.conflict {
                RecConflictPanel(conflict: conflict) { Task { await viewModel.load(quiet: true) } }
            }
            if let confidence = card.confidence {
                ConfidenceLine(confidence: confidence, recKey: card.key, surface: "marketing", module: "marketing")
            }
            Button {
                Haptic.light()
                viewModel.recordOpened(card.key)
                onDraft(card)
            } label: {
                Group {
                    if drafting {
                        CavnarShimmerText(text: "Drafting\u{2026}", color: primary ? .white : .cavnarInk)
                    } else {
                        Text("Draft it \u{2192}")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .modifier(OpportunityDraftButtonStyle(primary: primary))
            .disabled(draftingKey != nil)
            .accessibilityLabel("Draft it: \(card.title)")
        }
        .padding(CavnarSpace.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card)
            .strokeBorder(focused ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: focused ? 1.5 : 1))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}

/// "Draft it" is the one primary on the first card only (DESIGN_SYSTEM:
/// Opportunity Feed), secondary on the rest.
private struct OpportunityDraftButtonStyle: ViewModifier {
    let primary: Bool
    func body(content: Content) -> some View {
        if primary {
            content.buttonStyle(CavnarPrimaryButtonStyle())
        } else {
            content.buttonStyle(CavnarSecondaryButtonStyle())
        }
    }
}
