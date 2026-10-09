import SwiftUI

/// "Did we win today?" — the top of the Owner DSR (`dsr/scorecard.py`,
/// 9/25/26): the verdict, the overall score out of 100, sales · labor ·
/// food cost · guest experience, then (below the executive summary) Today's
/// wins and Today's risks. Every figure comes from the server's measured
/// blocks; a component that wasn't measured says why, never a zero. The
/// manager's view carries no scorecard.
struct DSRScorecard: Decodable, Hashable {
    struct Verdict: Decodable, Hashable {
        let label: String
        let tone: String?
    }

    struct Component: Decodable, Hashable, Identifiable {
        let key: String
        let label: String
        let measured: Bool
        let value: String?
        let detail: String?
        let tone: String?
        let why: String?
        var id: String { key }
    }

    struct Item: Decodable, Hashable, Identifiable {
        let text: String
        let key: String?
        var id: String { (key ?? "") + text }
    }

    let overall: Int?
    let verdict: Verdict?
    let components: [Component]
    let wins: [Item]
    let risks: [Item]
    let basis: String?
    /// The report names the night it is about ("Monday's score"), never
    /// "Today's" — it is read the morning after (owner, 9/29/26). The server
    /// sends the labels; the fallbacks are neutral.
    let labels: [String: String]

    func label(_ key: String, _ fallback: String) -> String { labels[key] ?? fallback }

    enum CodingKeys: String, CodingKey { case overall, verdict, components, wins, risks, basis, labels }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        overall = try? c.decodeIfPresent(Int.self, forKey: .overall)
        verdict = try? c.decodeIfPresent(Verdict.self, forKey: .verdict)
        components = (try? c.decodeIfPresent([Component].self, forKey: .components)) ?? []
        wins = (try? c.decodeIfPresent([Item].self, forKey: .wins)) ?? []
        risks = (try? c.decodeIfPresent([Item].self, forKey: .risks)) ?? []
        basis = try? c.decodeIfPresent(String.self, forKey: .basis)
        labels = (try? c.decodeIfPresent([String: String].self, forKey: .labels)) ?? [:]
    }

    static func color(_ tone: String?) -> Color? {
        switch tone {
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        case "bad": return .cavnarRed
        default: return nil
        }
    }
}

/// The night's score: the verdict and the overall score — the screen's ONE
/// FigureXL (DESIGN_SYSTEM §2) — the net (the one place the report states
/// it, 10/8/26) against budget, then the four components in a 2×2 grid.
struct DSRScorecardCard: View {
    let card: DSRScorecard
    /// The night's Sales block, for the net and net-vs-budget beside the
    /// verdict (the report's hero, density #2). Nil, or a block that isn't
    /// ready, draws neither; a view without the budget (a manager) has no
    /// `budget_net` and so no budget line.
    var sales: DSRBlock? = nil
    @State private var showingBasis = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Net minus budget, in dollars — only when both were measured.
    static func vsBudget(_ sales: DSRBlock?) -> Double? {
        guard let s = sales, s.isReady, let net = s.metric("net"), let budget = s.metric("budget_net") else {
            return nil
        }
        return net - budget
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            CavnarKicker(card.label("score", "The night's score"))
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                Circle()
                    .fill(DSRScorecard.color(card.verdict?.tone) ?? Color.cavnarInk3)
                    .frame(width: 12, height: 12)
                    .accessibilityHidden(true)
                Text(card.verdict?.label ?? "Not scored yet")
                    .cavnarText(.title)
                    .lineLimit(2)
                    .minimumScaleFactor(0.85)
                Spacer(minLength: CavnarSpace.xs)
                if let overall = card.overall {
                    // The screen's one FigureXL: the status (§2).
                    (Text("\(overall)").font(.cavnar(.figureXL)).foregroundColor(.cavnarInk)
                     + Text("/100").font(.cavnar(.secondary)).foregroundColor(.cavnarInk2))
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("Overall score \(overall) out of 100")
                }
            }
            if let s = sales, s.isReady, let net = s.metric("net") {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                    (Text(DSRFormat.money(net)).font(.cavnar(.figureM)).foregroundColor(.cavnarInk)
                     + Text(" net").font(.cavnar(.secondary)).foregroundColor(.cavnarInk2))
                        .cavnarSensitive()
                    if let vs = Self.vsBudget(s) {
                        Text("\(vs >= 0 ? "+" : "\u{2212}")\(DSRFormat.money(abs(vs))) vs budget")
                            .font(.cavnarNumber(CavnarType.secondary, weight: 700))
                            .foregroundStyle(vs >= 0 ? Color.cavnarGreen : Color.cavnarAmber)
                            .cavnarSensitive()
                    }
                    Spacer(minLength: 0)
                }
                .accessibilityElement(children: .combine)
            }
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10, alignment: .top),
                                GridItem(.flexible(), spacing: 10, alignment: .top)], spacing: 10) {
                ForEach(card.components) { c in component(c) }
            }
            // How the score is built, behind a tap (re-audit D20): the
            // hero says the verdict, not its method.
            if let basis = card.basis, !basis.isEmpty {
                Button {
                    Haptic.light()
                    if reduceMotion { showingBasis.toggle() } else {
                        withAnimation(.easeOut(duration: 0.2)) { showingBasis.toggle() }
                    }
                } label: {
                    HStack(spacing: CavnarSpace.xxs + 2) {
                        Text("How the score works").font(.cavnarBody(CavnarType.secondary, weight: 700))
                        Image(systemName: "chevron.down")
                            .font(.cavnar(.caption))
                            .rotationEffect(.degrees(showingBasis ? 180 : 0))
                            .accessibilityHidden(true)
                        Spacer(minLength: 0)
                    }
                    .foregroundStyle(Color.cavnarEmber2)
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityValue(showingBasis ? "Expanded" : "Collapsed")
                if showingBasis {
                    Text(basis).cavnarText(.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                        .transition(reduceMotion ? .identity : .opacity)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    @ViewBuilder
    private func component(_ c: DSRScorecard.Component) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker(c.label, isHeader: false)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
            if c.measured, let value = c.value {
                if c.key == "guests" {
                    // The stars at the Label role in the component's tone,
                    // like the other three (re-audit D10).
                    Text(value).cavnarText(.label, color: DSRScorecard.color(c.tone) ?? .cavnarInk)
                        .accessibilityLabel(c.detail ?? value)
                } else {
                    HomeMixedText.make(value, role: .label, color: DSRScorecard.color(c.tone) ?? .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let detail = c.detail {
                    HomeMixedText.make(detail, role: .caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text(c.why ?? "Not measured").cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.cavnarInk3.opacity(0.07)))
    }
}

/// The night's wins and risks in ONE card (10/8/26): the top risk first,
/// then the top win, the rest behind "+N more".
struct DSRWinsRisks: View {
    let card: DSRScorecard

    var body: some View {
        DSRWinsRisksCard(riskTitle: card.label("risks", "The night's risks"), risks: card.risks.map(\.text),
                         noRisks: card.label("no_risks", "Nothing to watch from that night."),
                         winTitle: card.label("wins", "The night's wins"), wins: card.wins.map(\.text),
                         noWins: card.label("no_wins", "Nothing stood out that night."))
    }
}

/// Risks above wins, one of each shown, the rest one tap away — the
/// owner's scorecard lists and a manager's needs-attention / went-well.
struct DSRWinsRisksCard: View {
    let riskTitle: String
    let risks: [String]
    var noRisks: String? = nil
    let winTitle: String
    let wins: [String]
    var noWins: String? = nil

    @State private var expanded = false

    private var hidden: Int { max(0, risks.count - 1) + max(0, wins.count - 1) }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            section(riskTitle, expanded ? risks : Array(risks.prefix(1)), glyph: "exclamationmark.triangle.fill",
                    tint: .cavnarAmber, empty: noRisks)
            section(winTitle, expanded ? wins : Array(wins.prefix(1)), glyph: "checkmark",
                    tint: .cavnarGreen, empty: noWins)
            if hidden > 0 {
                CavnarMoreToggle(hiddenCount: hidden, isExpanded: $expanded)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    @ViewBuilder
    private func section(_ title: String, _ items: [String], glyph: String, tint: Color, empty: String?) -> some View {
        if !items.isEmpty || empty != nil {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker(title)
                if items.isEmpty, let empty {
                    Text(empty).cavnarText(.secondary)
                }
                ForEach(Array(items.enumerated()), id: \.offset) { _, text in
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                        Image(systemName: glyph).font(.cavnar(.secondary)).foregroundStyle(tint)
                            .frame(width: 18)
                            .accessibilityHidden(true)
                        CavnarMixedText(text, role: .body)
                        Spacer(minLength: 0)
                    }
                }
            }
        }
    }
}
