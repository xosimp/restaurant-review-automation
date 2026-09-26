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
    @State private var explaining = false
    @State private var toast: String?

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
                HomeMixedText.make(item.title, size: 18, weight: 600, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if !item.detail.isEmpty {
                    HomeMixedText.make(item.detail, size: 13.5, weight: 500, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let evidence = item.evidenceLine {
                    HomeMixedText.make(evidence, size: 12.5, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let c = item.confidence {
                    ConfidenceLine(confidence: c, recKey: item.recKey, surface: "home", module: "home")
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
                                HomeMixedText.make(cta, size: 14.5, weight: 700, color: .white,
                                                   numberWeight: 700, numberColor: .white)
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(busy && item.isPublishAction)
                }
                HomeAskLink(question: "What should I do about this: \(item.title)")
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
                HomeMixedText.make(rec.title, size: 18, weight: 600, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if rec.modelWritten == true { ClaimKindTag(kind: nil, modelWritten: true) }
                if let why = rec.why, !why.isEmpty {
                    HomeMixedText.make(why, size: 13.5, weight: 500, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let c = rec.confidence {
                    ConfidenceLine(confidence: c, recKey: rec.key, surface: "home", module: rec.module ?? "home")
                }
                if let line = RecDollarCalibration.line(raw: rec.dollarsMonthly, adjusted: rec.dollarsAdjusted,
                                                        n: rec.calibrationN, note: rec.calibrationNote) {
                    HomeMixedText.make(line, size: 15, weight: 600, color: .cavnarInk, numberWeight: 600)
                    if let basis = rec.dollarsBasis {
                        HomeMixedText.make(basis, size: 12, weight: 500, color: .cavnarInk3)
                    }
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
                RecAnswerRow(key: rec.key, surface: "home", module: rec.module ?? "home",
                             answers: [.completed, .notForUs])
                if let toast {
                    Text(toast)
                        .font(.cavnarBody(12.5, weight: 600))
                        .foregroundStyle(Color.cavnarGreen)
                }
                HomeAskLink(question: "Walk me through this: \(rec.title)")
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
                VStack(alignment: .leading, spacing: 10) {
                    if let modules = ff.modules, modules.count > 1 {
                        HStack(spacing: 8) {
                            ForEach(Array(modules.enumerated()), id: \.offset) { index, module in
                                if index > 0 { EmberThread(axis: .horizontal, length: 22) }
                                Text(RecSummaryFormat.moduleLabel(module).uppercased())
                                    .font(.cavnarBody(10.5, weight: 700))
                                    .tracking(1.0)
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                    HomeMixedText.make(what, size: 18, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    // What kind of claim this is (K4) — measured, computed,
                    // forecast, inferred.
                    ClaimKindTag(kind: ff.claimKind)
                    if let why = ff.why, !why.isEmpty {
                        HomeMixedText.make(why.prefix(1).uppercased() + why.dropFirst(), size: 13.5, weight: 500,
                                           color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let confirm = ff.confirmBy, !confirm.isEmpty, confirm != what {
                        HomeMixedText.make("To confirm: " + confirm, size: 13, weight: 600, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let evidence = ff.evidence?.prefix(3), !evidence.isEmpty {
                        VStack(alignment: .leading, spacing: 3) {
                            ForEach(Array(evidence.enumerated()), id: \.offset) { _, line in
                                HomeMixedText.make("\u{00B7} " + line, size: 12.5, weight: 500, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    // How sure — the most prominent item on Home carried a
                    // confidence it never showed (CA4, CA1 H8).
                    if let c = ff.confidence {
                        ConfidenceLine(confidence: c, recKey: ff.answerKey, surface: "home", module: "home")
                    }
                    // The money fallback is a range with its label — never
                    // one figure pulled out of it (CA4 F3).
                    if let range = ff.moneyRange {
                        VStack(alignment: .leading, spacing: 2) {
                            HomeMixedText.make(range, size: 19, weight: 600, color: .cavnarInk,
                                               numberWeight: 600)
                            if let label = ff.money?.label, !label.isEmpty {
                                HomeMixedText.make(label, size: 12.5, weight: 600, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                    HStack(alignment: .firstTextBaseline, spacing: 14) {
                        // Calibrated by this restaurant's measured results
                        // when the server sent it (F6), and said so.
                        if let dollars = ff.statedDollars {
                            VStack(alignment: .leading, spacing: 2) {
                                (Text("$" + dollars.commaFormatted).font(.cavnarNumber(19, weight: 600))
                                    .foregroundColor(.cavnarInk)
                                 + Text("/month").font(.cavnarBody(12.5, weight: 600)).foregroundColor(.cavnarInk3))
                                if let note = ff.dollarsNote {
                                    HomeMixedText.make(note, size: 12, weight: 500, color: .cavnarInk3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                // What the figure covers (B4 H7).
                                if let basis = ff.dollarsBasis {
                                    HomeMixedText.make(basis, size: 12, weight: 500, color: .cavnarInk3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        Spacer(minLength: 0)
                        if ff.alternative != nil {
                            Button {
                                Haptic.light()
                                explaining = true
                                RecEvidenceLog.viewed(key: ff.answerKey, surface: "home", module: "home")
                            } label: {
                                Text("Could also be\u{2026}")
                                    .font(.cavnarBody(13, weight: 600))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    HomeAskLink(question: "Walk me through this: \(what)")
                    if ff.answerable == true, let key = ff.answerKey {
                        RecAnswerRow(key: key, surface: "home", module: "home")
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
                    if let modules = link.modules, !modules.isEmpty {
                        HStack(spacing: 8) {
                            EmberThread(axis: .horizontal, length: 34)
                            Text(modules.map { RecSummaryFormat.moduleLabel($0) }.joined(separator: " + ").uppercased())
                                .font(.cavnarBody(11, weight: 700))
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
