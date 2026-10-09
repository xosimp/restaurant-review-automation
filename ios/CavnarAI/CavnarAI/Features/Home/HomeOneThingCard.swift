import SwiftUI

/// "Today's focus" — Home's one pick, on the iPhone answer card
/// (CavnarAnswerCard, iOS readability round 10/8/26, #6): the headline,
/// one sentence of why, the top cause (labelled a hypothesis when no
/// measured signal backs it), "Could also be" as ONE visible line, the one
/// dollar figure with what it covers, the confidence %, and ONE full-width
/// primary. Everything else — the modules it joins, what it rests on, what
/// would confirm it, the notes on the figure, advice it pulls against and
/// Done / Pass — is behind "See the evidence".
///
/// What leads is the web's `renderFocus` order (parity audit #1,
/// `HomeFocusLead.pick`): the cross-module finding
/// (GET /mobile/api/cross-module → fix_first,
/// business_intelligence.pick_one_thing), else the most urgent
/// Needs-attention item — whose action is then the page's one primary, and
/// which Needs you under it no longer repeats — else the top recommendation.
struct HomeOneThingCard: View {
    let viewModel: HomeFollowThroughViewModel
    /// What leads; nil draws the finding when there is one (older callers).
    var lead: HomeFocusLead? = nil
    var busy: Bool = false
    /// An attention lead's action — Needs you's own handler (publish asks
    /// first, anything else opens its nav).
    var onPrimary: (NeedsAttentionItem) -> Void = { _ in }
    /// Something changed server-side (a conflict settled) — reload Home.
    var onChanged: () -> Void = {}
    @State private var toast: String?
    @State private var answering = false
    @Environment(DeepLinkRouter.self) private var router

    var body: some View {
        Group {
            switch lead {
            case .attention(let item)?: attentionCard(item)
            case .recommendation(let rec)?: recommendationCard(rec)
            default: findingCard
            }
        }
        // The answer's sentence is a confirmation, not a fixture (iOS
        // re-audit L7): it clears after a few seconds.
        .task(id: toast) {
            guard toast != nil else { return }
            try? await Task.sleep(for: .seconds(4))
            withAnimation(.cavnarEase(0.3)) { toast = nil }
        }
    }

    static let kicker = "Today\u{2019}s focus"

    /// The one primary, full width, ember.
    private func primaryButton(_ label: String, working: Bool = false, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.medium()
            action()
        } label: {
            Group {
                if working {
                    CavnarShimmerText(text: "Working\u{2026}", color: .white)
                } else {
                    HomeMixedText.make(label, role: .label, color: .white, numberColor: .white)
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
        .disabled(working)
    }

    private func ask(_ question: String) {
        router.pendingAskScreen = AskScreen(panel: "home")
        router.pendingAskAutoSend = true
        router.pendingAskPrompt = question
    }

    /// The one dollar figure and, always under it, what it covers (B4 H7).
    private func figure(_ text: String, basis: String?) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarMixedText(text, role: .figureM)
            if let basis, !basis.isEmpty {
                CavnarMixedText(basis, role: .caption)
            }
        }
        .accessibilityElement(children: .combine)
        .cavnarSensitive()
    }

    // MARK: - An attention item leads

    private func attentionCard(_ item: NeedsAttentionItem) -> some View {
        CavnarAnswerCard(
            kicker: Self.kicker,
            headline: item.title,
            summary: item.detail.isEmpty ? nil : item.detail,
            confidence: item.confidence.map {
                ConfidenceLine(confidence: $0, recKey: item.recKey, surface: "home", module: "home", compact: true)
            },
            surface: .hero
        ) {
            if let cta = item.cta {
                primaryButton(cta, working: busy && item.isPublishAction) { onPrimary(item) }
            } else {
                primaryButton("Ask Cavnar AI") { ask("What should I do about this: \(item.title)") }
            }
        } detail: {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if let evidence = item.evidenceLine {
                    CavnarMixedText(evidence, role: .secondary)
                }
                RecMemoryNote(previous: item.previousAnswer, delegate: item.delegateAnswer)
                if let conflict = item.conflict {
                    RecConflictPanel(conflict: conflict, onSettled: { Task { await viewModel.load() } })
                }
                if item.cta != nil {
                    HomeAskLink(question: "What should I do about this: \(item.title)", label: "Ask Cavnar AI")
                }
            }
        }
    }

    // MARK: - The top recommendation leads

    private func recommendationCard(_ rec: HomeRecommendation) -> some View {
        let dollars = RecDollarCalibration.line(raw: rec.dollarsMonthly, adjusted: rec.dollarsAdjusted,
                                                n: rec.calibrationN, note: rec.calibrationNote)
        return CavnarAnswerCard(
            kicker: Self.kicker,
            headline: rec.title,
            summary: rec.why,
            alternativeCause: rec.alternative,
            confidence: rec.confidence.map {
                ConfidenceLine(confidence: $0, recKey: rec.key, surface: "home", module: rec.module ?? "home",
                               compact: true)
            },
            surface: .hero
        ) {
            // Another module's word against it changes how the owner
            // answers, so it stays on the face (M3's trim guard).
            if let caution = rec.caution {
                RecCautionLine(text: caution)
            }
            if let dollars {
                figure(dollars, basis: rec.dollarsBasis)
            }
            // The decision leads (iOS re-audit L7): the card's own action
            // (Reprice) or Done. "Measure it" — only on a card that names a
            // metric, one that doesn't can't be measured before and after —
            // is the secondary beside it.
            if let a = rec.action, a.kind == "reprice" {
                primaryButton(a.label ?? "Reprice", working: answering) {
                    Task {
                        answering = true
                        defer { answering = false }
                        if let message = await viewModel.reprice(rec) {
                            withAnimation(.cavnarEase(0.25)) { toast = message }
                            onChanged()
                        }
                    }
                }
            } else {
                primaryButton("Done", working: answering) {
                    Task {
                        answering = true
                        defer { answering = false }
                        if let message = await viewModel.answer(rec, kind: "done") {
                            withAnimation(.cavnarEase(0.25)) { toast = message }
                            onChanged()
                        }
                    }
                }
            }
            if rec.metric != nil {
                Button {
                    Haptic.light()
                    Task {
                        if let message = await viewModel.track(rec) {
                            withAnimation(.cavnarEase(0.25)) { toast = message }
                        }
                    }
                } label: {
                    Text(viewModel.tracked.contains(rec.key) ? "Measuring" : "Measure it")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.tracked.contains(rec.key))
                .accessibilityHint("Takes a baseline now and measures the result in a few weeks")
            }
            if let toast {
                Text(toast).cavnarText(.secondary, color: .cavnarGreen)
                    .transition(.opacity)
            }
        } detail: {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if rec.modelWritten == true {
                    Text("Written by Cavnar AI from your numbers").cavnarText(.caption)
                }
                RecMemoryNote(previous: rec.previousAnswer, delegate: rec.delegateAnswer,
                              retest: rec.retest == true)
                if let conflict = rec.conflict {
                    RecConflictPanel(conflict: conflict, onSettled: onChanged)
                }
                // Done is the face's primary (L7); Not for us stays here.
                RecAnswerRow(key: rec.key, surface: "home", module: rec.module ?? "home",
                             answers: [.notForUs], onAnswered: { _ in onChanged() })
                HomeAskLink(question: "Walk me through this: \(rec.title)", label: "Ask Cavnar AI")
            }
            .onAppear { RecEvidenceLog.viewed(key: rec.key, surface: "home", module: rec.module ?? "home") }
        }
    }

    // MARK: - The cross-module finding leads

    @ViewBuilder
    private var findingCard: some View {
        if let ff = viewModel.fixFirst, let what = ff.what, !what.isEmpty {
            let why = ff.why.map { $0.prefix(1).uppercased() + $0.dropFirst() }
            CavnarAnswerCard(
                kicker: Self.kicker,
                headline: what,
                summary: why,
                cause: ff.evidence?.first,
                isHypothesis: Self.isHypothesis(ff.claimKind),
                alternativeCause: ff.alternative,
                confidence: ff.confidence.map {
                    ConfidenceLine(confidence: $0, recKey: ff.answerKey, surface: "home", module: "home", compact: true)
                },
                surface: .hero
            ) {
                // Calibrated by this restaurant's measured results when the
                // server sent it (F6); the money fallback is a range with its
                // label — never one figure pulled out of it (CA4 F3).
                if let dollars = ff.statedDollars {
                    figure("$" + dollars.commaFormatted + "/month", basis: ff.dollarsBasis)
                } else if let range = ff.moneyRange {
                    figure(range, basis: ff.money?.label)
                }
                primaryButton("Walk me through it") { ask("Walk me through this: \(what)") }
            } detail: {
                findingDetails(ff, what: what)
            }
        }
    }

    /// A cause no measured signal backs reads "Hypothesis".
    static func isHypothesis(_ claimKind: String?) -> Bool {
        switch claimKind?.lowercased() {
        case "inferred", "suggestion", "estimate": return true
        default: return false
        }
    }
}

extension HomeOneThingCard {
    /// The finding's proof, behind "See the evidence": the modules it joins
    /// (the ember thread only when there are two — a real link, never
    /// decoration), what kind of claim it is, the rest of what it rests on,
    /// what would confirm it, the note on the figure, advice it pulls
    /// against, and Done / Pass. Opening it records that the owner looked.
    @ViewBuilder
    fileprivate func findingDetails(_ ff: HomeFollowThroughViewModel.CrossModule.FixFirst, what: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            if let modules = ff.modules, modules.count > 1 {
                HStack(spacing: CavnarSpace.xs) {
                    ForEach(Array(modules.enumerated()), id: \.offset) { index, module in
                        if index > 0 { EmberThread(axis: .horizontal, length: 22) }
                        Text(RecSummaryFormat.moduleLabel(module))
                            .cavnarText(.kicker, color: .cavnarInk2)
                    }
                }
                .accessibilityElement(children: .combine)
            }
            if let label = ClaimKind.label(kind: ff.claimKind) {
                Text("How Cavnar AI knows: \(label.lowercased())").cavnarText(.caption)
            }
            if let evidence = ff.evidence?.dropFirst().prefix(3), !evidence.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    ForEach(Array(evidence.enumerated()), id: \.offset) { _, line in
                        CavnarMixedText("\u{00B7} " + line, role: .secondary)
                    }
                }
            }
            if let confirm = ff.confirmBy, !confirm.isEmpty, confirm != what {
                CavnarMixedText("To confirm: " + confirm, role: .secondary)
            }
            if let note = ff.dollarsNote {
                CavnarMixedText(note, role: .secondary, color: .cavnarAmber)
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
        }
        .onAppear { RecEvidenceLog.viewed(key: ff.answerKey, surface: "home", module: "home") }
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
                    HomeMixedText.make(link.headline, size: CavnarType.emphasis, weight: 700, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    // How long it has stood (memory round: link_memory) —
                    // "Found 3 weeks running, since 9/7/26" — and the
                    // recurring badge when it keeps coming back.
                    if let memory = link.memory, let line = memory.line {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            if let badge = memory.badge {
                                AccountChip(text: badge, tint: .cavnarAmber)
                            }
                            HomeMixedText.make(line, size: CavnarType.caption, weight: 600, color: .cavnarInk3)
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
                                HomeMixedText.make("\u{00B7} " + line, size: CavnarType.caption, weight: 500, color: .cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    if let confirm = link.confirmBy {
                        HomeMixedText.make("To confirm: " + confirm, size: CavnarType.caption, weight: 600, color: .cavnarInk2)
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
