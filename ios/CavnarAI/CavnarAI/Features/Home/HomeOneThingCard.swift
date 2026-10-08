import SwiftUI

/// "If you only do one thing" — Home's cross-module pick
/// (GET /mobile/api/cross-module → fix_first), in the slot DESIGN_SYSTEM
/// §11b gives it: after Needs attention, before the recommendations. The
/// web's focus card has always led with it; the phone never showed it, so
/// the one recommendation both Homes present was the one iOS could not
/// answer (rec-ROI #26).
///
/// Always an action (business_intelligence.pick_one_thing). The modules it
/// joins are drawn with the ember thread only when there are two — a real
/// link in the payload, never decoration. "Could also be…" opens what else
/// would explain it, and records that the owner looked (#38).
///
/// When there is no finding it leads with what the web's focus card leads
/// with, in the same order (parity audit #1, web `renderFocus`): the most
/// urgent Needs-attention item — whose action is then the page's one
/// primary, and which the deck under it no longer repeats — else the top
/// recommendation. See `HomeFocusLead.pick`.
struct HomeOneThingCard: View {
    let viewModel: HomeFollowThroughViewModel
    /// What leads; nil draws the finding when there is one (older callers).
    var lead: HomeFocusLead? = nil
    var busy: Bool = false
    /// An attention lead's action — the deck's own handler (publish asks
    /// first, anything else opens its nav).
    var onPrimary: (NeedsAttentionItem) -> Void = { _ in }
    /// Something changed server-side (a conflict settled) — reload Home.
    var onChanged: () -> Void = {}
    @State private var explaining = false
    @State private var toast: String?
    /// The card's second layer (HomeCardKit): evidence, how sure, the
    /// basis, what else could explain it.
    @State private var detailsOpen = false

    var body: some View {
        switch lead {
        case .attention(let item)?: attentionCard(item)
        case .recommendation(let rec)?: recommendationCard(rec)
        default: findingCard
        }
    }

    // MARK: - An attention item leads

    private func attentionCard(_ item: NeedsAttentionItem) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HomeSectionHeader(kicker: "Start here", title: "Today\u{2019}s focus")
            VStack(alignment: .leading, spacing: 10) {
                HomeMixedText.make(item.title, size: HomeType.title + 1, weight: 600, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if !item.detail.isEmpty {
                    HomeClampedText(text: item.detail, size: HomeType.body, color: .cavnarInk2, lines: 3)
                }
                if detailsOpen {
                    VStack(alignment: .leading, spacing: 8) {
                        if let evidence = item.evidenceLine {
                            HomeMixedText.make(evidence, size: HomeType.meta, weight: 500, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let c = item.confidence {
                            ConfidenceLine(confidence: c, recKey: item.recKey, surface: "home", module: "home")
                        }
                        RecMemoryNote(previous: item.previousAnswer, delegate: item.delegateAnswer)
                    }
                    .transition(.opacity)
                }
                if let conflict = item.conflict {
                    RecConflictPanel(conflict: conflict, onSettled: { Task { await viewModel.load() } })
                }
                if let cta = item.cta {
                    Button {
                        Haptic.medium()
                        onPrimary(item)
                    } label: {
                        Group {
                            if busy && item.isPublishAction {
                                CavnarShimmerText(text: "Working\u{2026}", color: .white)
                            } else {
                                HomeMixedText.make(cta, size: 16, weight: 700, color: .white,
                                                   numberWeight: 700, numberColor: .white)
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(busy && item.isPublishAction)
                }
                HStack(spacing: 22) {
                    HomeAskLink(question: "What should I do about this: \(item.title)", label: "Ask")
                    if item.evidenceLine != nil || item.confidence != nil
                        || item.previousAnswer != nil || item.delegateAnswer != nil {
                        HomeDetailsToggle(open: $detailsOpen)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard(.hero)
        }
    }

    // MARK: - The top recommendation leads

    private func recommendationCard(_ rec: HomeRecommendation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HomeSectionHeader(kicker: "Start here", title: "Today\u{2019}s focus")
            VStack(alignment: .leading, spacing: 10) {
                HomeMixedText.make(rec.title, size: HomeType.title + 1, weight: 600, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if let caution = rec.caution {
                    RecCautionLine(text: caution)
                }
                if let why = rec.why, !why.isEmpty {
                    HomeClampedText(text: why, size: HomeType.body, color: .cavnarInk2, lines: 3)
                }
                if let line = RecDollarCalibration.line(raw: rec.dollarsMonthly, adjusted: rec.dollarsAdjusted,
                                                        n: rec.calibrationN, note: rec.calibrationNote) {
                    HomeMixedText.make(line, size: 18, weight: 600, color: .cavnarInk, numberWeight: 600)
                    if let basis = rec.dollarsBasis {
                        HomeMixedText.make(basis, size: HomeType.meta, weight: 500, color: .cavnarInk3)
                            .lineLimit(2)
                    }
                }
                if detailsOpen {
                    VStack(alignment: .leading, spacing: 8) {
                        if rec.modelWritten == true { ClaimKindTag(kind: nil, modelWritten: true) }
                        if let c = rec.confidence {
                            ConfidenceLine(confidence: c, recKey: rec.key, surface: "home", module: rec.module ?? "home")
                        }
                        RecMemoryNote(previous: rec.previousAnswer, delegate: rec.delegateAnswer,
                                      retest: rec.retest == true)
                    }
                    .transition(.opacity)
                }
                // "Measure it" only on a card that names a metric — one that
                // doesn't can't be measured before and after.
                if rec.metric != nil {
                    Button {
                        Haptic.medium()
                        Task {
                            if let message = await viewModel.track(rec) { withAnimation { toast = message } }
                        }
                    } label: {
                        Text(viewModel.tracked.contains(rec.key) ? "Measuring" : "Measure it")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(viewModel.tracked.contains(rec.key))
                }
                if let conflict = rec.conflict {
                    RecConflictPanel(conflict: conflict, onSettled: onChanged)
                }
                RecAnswerRow(key: rec.key, surface: "home", module: rec.module ?? "home",
                             answers: [.completed, .notForUs])
                if let toast {
                    Text(toast)
                        .font(.cavnarBody(HomeType.meta + 1, weight: 600))
                        .foregroundStyle(Color.cavnarGreen)
                }
                HStack(spacing: 22) {
                    HomeAskLink(question: "Walk me through this: \(rec.title)", label: "Ask")
                    if rec.confidence != nil || rec.modelWritten == true
                        || rec.previousAnswer != nil || rec.delegateAnswer != nil {
                        HomeDetailsToggle(open: $detailsOpen)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard(.hero)
        }
    }

    // MARK: - The cross-module finding leads

    @ViewBuilder
    private var findingCard: some View {
        if let ff = viewModel.fixFirst, let what = ff.what, !what.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                HomeSectionHeader(kicker: "Start here", title: "If you only do one thing")
                VStack(alignment: .leading, spacing: 12) {
                    // The phone shows the pick, why, and what it is worth;
                    // the rest is under Details (HomeCardKit, 10/8/26).
                    HomeMixedText.make(what, size: HomeType.title + 1, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if let why = ff.why, !why.isEmpty {
                        HomeClampedText(text: why.prefix(1).uppercased() + why.dropFirst(),
                                        size: HomeType.body, color: .cavnarInk2, lines: 3)
                    }
                    // Calibrated by this restaurant's measured results when
                    // the server sent it (F6); what the figure covers (B4
                    // H7) always rides under it — never a bare number.
                    if let dollars = ff.statedDollars {
                        VStack(alignment: .leading, spacing: 3) {
                            (Text("$" + dollars.commaFormatted).font(.cavnarNumber(26, weight: 600))
                                .foregroundColor(.cavnarInk)
                             + Text("/month").font(.cavnarBody(15, weight: 600)).foregroundColor(.cavnarInk3))
                            if let basis = ff.dollarsBasis {
                                HomeMixedText.make(basis, size: HomeType.meta, weight: 500, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    } else if let range = ff.moneyRange {
                        // The money fallback is a range with its label —
                        // never one figure pulled out of it (CA4 F3).
                        VStack(alignment: .leading, spacing: 3) {
                            HomeMixedText.make(range, size: 22, weight: 600, color: .cavnarInk, numberWeight: 600)
                            if let label = ff.money?.label, !label.isEmpty {
                                HomeMixedText.make(label, size: HomeType.meta, weight: 500, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                    if detailsOpen {
                        findingDetails(ff, what: what)
                            .transition(.opacity)
                    }
                    // Advice the hero pulls against (memory round 9/29/26).
                    if let conflict = ff.conflict {
                        RecConflictPanel(conflict: conflict, onSettled: {
                            Task { await viewModel.load() }
                            onChanged()
                        })
                    }
                    if ff.answerable == true, let key = ff.answerKey {
                        RecAnswerRow(key: key, surface: "home", module: "home")
                    }
                    HStack(spacing: 22) {
                        HomeAskLink(question: "Walk me through this: \(what)", label: "Ask")
                        HomeDetailsToggle(open: $detailsOpen)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard(.hero)
            }
            .alert("What else could explain it", isPresented: $explaining) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(ff.alternative ?? "")
            }
        }
    }
}

extension HomeOneThingCard {
    /// The finding's second layer: the modules it joins, what kind of
    /// claim it is (K4), how sure (CA4), what it rests on, what would
    /// confirm it, the note on the figure, and what else could explain it.
    @ViewBuilder
    fileprivate func findingDetails(_ ff: HomeFollowThroughViewModel.CrossModule.FixFirst, what: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let modules = ff.modules, modules.count > 1 {
                HStack(spacing: 8) {
                    ForEach(Array(modules.enumerated()), id: \.offset) { index, module in
                        if index > 0 { EmberThread(axis: .horizontal, length: 22) }
                        Text(RecSummaryFormat.moduleLabel(module).uppercased())
                            .font(.cavnarBody(CavnarType.kicker + 1, weight: 700))
                            .tracking(1.0)
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                .accessibilityElement(children: .combine)
            }
            ClaimKindTag(kind: ff.claimKind)
            if let c = ff.confidence {
                ConfidenceLine(confidence: c, recKey: ff.answerKey, surface: "home", module: "home")
            }
            if let evidence = ff.evidence?.prefix(3), !evidence.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(Array(evidence.enumerated()), id: \.offset) { _, line in
                        HomeMixedText.make("\u{00B7} " + line, size: HomeType.meta + 1, weight: 500, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            if let confirm = ff.confirmBy, !confirm.isEmpty, confirm != what {
                HomeMixedText.make("To confirm: " + confirm, size: HomeType.meta + 1, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let note = ff.dollarsNote {
                HomeMixedText.make(note, size: HomeType.meta, weight: 500, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let alternative = ff.alternative {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Could also be")
                        .font(.cavnarBody(HomeType.meta, weight: 700))
                        .foregroundStyle(Color.cavnarInk3)
                    Text(alternative)
                        .font(.cavnarBody(HomeType.meta + 1))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .onAppear { RecEvidenceLog.viewed(key: ff.answerKey, surface: "home", module: "home") }
            }
        }
    }
}

/// What Home's one-thing card leads with — the web's `renderFocus` order:
/// the cross-module finding, else the most urgent Needs-attention item,
/// else the top recommendation. Nil until the day's reads have landed
/// (the web holds a placeholder then: an item promoted for a second and
/// then replaced read as a banner that vanished), and nil when nothing
/// needs the owner — Needs attention's all-clear row says that.
enum HomeFocusLead {
    case finding
    case attention(NeedsAttentionItem)
    case recommendation(HomeRecommendation)

    static func pick(loaded: Bool, hasFinding: Bool, attention: [NeedsAttentionItem],
                     recommendations: [HomeRecommendation]) -> HomeFocusLead? {
        guard loaded else { return nil }
        if hasFinding { return .finding }
        if let first = attention.first { return .attention(first) }
        if let top = recommendations.first { return .recommendation(top) }
        return nil
    }

    /// "finding" / "attention" / "recommendation" — for tests and logs.
    var kind: String {
        switch self {
        case .finding: return "finding"
        case .attention: return "attention"
        case .recommendation: return "recommendation"
        }
    }

    /// The key the lead is answered under — what the brief leaves out.
    var key: String? {
        switch self {
        case .finding: return nil
        case .attention(let item): return item.recKey ?? item.type
        case .recommendation(let rec): return rec.key
        }
    }
}

/// A cross-module link's Evidence, opened from its Needs-attention row
/// (What connects is no longer a card on Home — parity audit #5): what it
/// rests on, what would confirm it, what else would explain it, Ask, and
/// Done / Not for us. A question, not a finding.
struct HomeLinkEvidenceSheet: View {
    let link: HomeFollowThroughViewModel.CrossModule.Link

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    HomeMixedText.make(link.headline, size: 17, weight: 700, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    // How long it has stood (memory round: link_memory) —
                    // "Found 3 weeks running, since 9/7/26" — and the
                    // recurring badge when it keeps coming back.
                    if let memory = link.memory, let line = memory.line {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            if let badge = memory.badge {
                                AccountChip(text: badge, tint: .cavnarAmber)
                            }
                            HomeMixedText.make(line, size: 13, weight: 600, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if let modules = link.modules, !modules.isEmpty {
                        HStack(spacing: 8) {
                            EmberThread(axis: .horizontal, length: 34)
                            Text(modules.map { RecSummaryFormat.moduleLabel($0) }.joined(separator: " + ").uppercased())
                                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                                .tracking(1.0)
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    if let evidence = link.evidence, !evidence.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(evidence, id: \.self) { line in
                                HomeMixedText.make("\u{00B7} " + line, size: 13.5, weight: 500, color: .cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    if let confirm = link.confirmBy {
                        HomeMixedText.make("To confirm: " + confirm, size: 13.5, weight: 600, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let alt = link.notACause ?? link.alternative {
                        CavnarCaveat(title: "A question, not a finding", detail: alt)
                    }
                    HomeAskLink(question: link.ask ?? "Tell me more about this: \(link.headline)")
                    if link.answerable == true, let key = link.recKey {
                        RecAnswerRow(key: key, surface: "home", module: "home")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard(.ai)
                .padding(20)
            }
            .background(Color.cavnarPaper.ignoresSafeArea())
            .accountSheetChrome("Evidence")
        }
        .presentationDetents([.medium, .large])
    }
}
