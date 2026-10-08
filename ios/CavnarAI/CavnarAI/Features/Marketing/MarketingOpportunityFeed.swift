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

struct MarketingOpportunitySection: View {
    let viewModel: MarketingOpportunityViewModel
    /// The card whose "Draft it" is drafting, if any — every other waits.
    var draftingKey: String?
    var onDraft: (MarketingOpportunity) -> Void

    @State private var rowHeights: [String: CGFloat] = [:]
    @State private var reasonFor: MarketingOpportunity?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Cavnar AI found", title: "Opportunities this week")
            if viewModel.isLoading && viewModel.items.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    CavnarSkeletonBar(height: 3)
                    Text("Looking for this week\u{2019}s opportunities")
                        .font(.cavnarBody(CavnarType.caption))
                        .foregroundStyle(Color.cavnarInk3)
                }
            } else if let error = viewModel.loadError {
                HStack(spacing: 10) {
                    Text(error).font(.cavnarBody(CavnarType.secondary)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer()
                    Button("Retry") { Task { await viewModel.load() } }
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
            } else if viewModel.loaded && viewModel.items.isEmpty {
                let line = viewModel.emptyLine
                CampaignCheckLine(ok: !line.warn, text: line.text)
            } else if !viewModel.items.isEmpty {
                let cards = viewModel.shown
                List {
                    ForEach(Array(cards.enumerated()), id: \.element.id) { i, card in
                        cardView(card, primary: i == 0)
                            .cavnarReportsRowHeight(card.key, into: $rowHeights)
                            .listRowBackground(Color.clear)
                            .listRowInsets(EdgeInsets(top: 6, leading: 0, bottom: 6, trailing: 0))
                            .listRowSeparator(.hidden)
                            .swipeActions(edge: .leading, allowsFullSwipe: false) {
                                Button {
                                    Task { await viewModel.answer(card, .completed) }
                                } label: { Label(RecAnswer.completed.label, systemImage: "checkmark") }
                                .tint(Color.cavnarGreen)
                            }
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                Button {
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
                HStack(spacing: 12) {
                    if viewModel.hiddenCount > 0 {
                        Button("Show \(viewModel.hiddenCount) more") {
                            Haptic.light()
                            Task { await viewModel.showMore() }
                        }
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 36)
                    }
                    if !viewModel.badSources.isEmpty {
                        Text("Couldn\u{2019}t check \(viewModel.badSources.joined(separator: ", ")) just now.")
                            .font(.cavnarBody(CavnarType.caption))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                Text("Swipe a card right for Done, left for Not for us.")
                    .font(.cavnarBody(CavnarType.caption))
                    .foregroundStyle(Color.cavnarInk3)
            }
            if let error = viewModel.answerError {
                Text(error).font(.cavnarBody(CavnarType.caption)).foregroundStyle(Color.cavnarRed)
            }
        }
    }

    private func cardView(_ card: MarketingOpportunity, primary: Bool) -> some View {
        let focused = viewModel.focusedKey == card.key
        let drafting = draftingKey == card.key
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(card.kindLabel.uppercased())
                    .font(.cavnarBody(CavnarType.kicker, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                if let when = card.whenLabel {
                    Text(when).font(.cavnarBody(CavnarType.caption, weight: 600)).foregroundStyle(Color.cavnarInk3)
                }
                if card.rankedByResults {
                    AccountChip(text: "Ranked by your results", muted: true)
                }
                Spacer(minLength: 0)
            }
            HomeMixedText.make(card.title, size: CavnarType.emphasis, weight: 700, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            if let why = card.why {
                HomeMixedText.make(why, size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if card.stakeLine != nil || !card.facts.isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 6) {
                        if let stake = card.stakeLine {
                            HomeMixedText.make(stake, size: 12.5, weight: 700, color: .cavnarInk, numberColor: .cavnarInk)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(Capsule().fill(Color.cavnarEmber.opacity(0.16)))
                        }
                        ForEach(card.facts, id: \.self) { fact in
                            HomeMixedText.make(fact, size: 12.5, weight: 600, color: .cavnarInk2)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(Capsule().fill(Color.white.opacity(0.05)))
                        }
                    }
                }
                .scrollIndicators(.hidden)
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
        .padding(14)
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
