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

    enum CodingKeys: String, CodingKey { case overall, verdict, components, wins, risks, basis }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        overall = try? c.decodeIfPresent(Int.self, forKey: .overall)
        verdict = try? c.decodeIfPresent(Verdict.self, forKey: .verdict)
        components = (try? c.decodeIfPresent([Component].self, forKey: .components)) ?? []
        wins = (try? c.decodeIfPresent([Item].self, forKey: .wins)) ?? []
        risks = (try? c.decodeIfPresent([Item].self, forKey: .risks)) ?? []
        basis = try? c.decodeIfPresent(String.self, forKey: .basis)
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

/// Today's score: the verdict and the overall score, then the four
/// components in a 2×2 grid.
struct DSRScorecardCard: View {
    let card: DSRScorecard
    /// The night's Sales block, for the net and net-vs-budget beside the
    /// verdict (the report's hero, density #2). Nil, or a block that isn't
    /// ready, draws neither; a view without the budget (a manager) has no
    /// `budget_net` and so no budget line.
    var sales: DSRBlock? = nil

    /// Net minus budget, in dollars — only when both were measured.
    static func vsBudget(_ sales: DSRBlock?) -> Double? {
        guard let s = sales, s.isReady, let net = s.metric("net"), let budget = s.metric("budget_net") else {
            return nil
        }
        return net - budget
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            DSRKicker(text: "Today's score")
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Circle()
                    .fill(DSRScorecard.color(card.verdict?.tone) ?? Color.cavnarInk3)
                    .frame(width: 12, height: 12)
                Text(card.verdict?.label ?? "Not scored yet")
                    .font(.cavnarHeadline(24))
                    .foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 8)
                if let overall = card.overall {
                    VStack(alignment: .trailing, spacing: 2) {
                        // The screen's one 40pt figure: the status (§2).
                        (Text("\(overall)").font(.cavnarNumber(CavnarType.heroNumber, weight: 600)).foregroundColor(.cavnarInk)
                         + Text("/100").font(.cavnarNumber(14)).foregroundColor(.cavnarInk3))
                        Text("OVERALL").font(.cavnarBody(10.5, weight: 700)).tracking(1.2)
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Overall performance \(overall) out of 100")
                }
            }
            if let s = sales, s.isReady, let net = s.metric("net") {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    (Text(DSRFormat.money(net)).font(.cavnarNumber(CavnarType.tileNumber, weight: 600))
                        .foregroundColor(.cavnarInk)
                     + Text(" net").font(.cavnarBody(CavnarType.secondary)).foregroundColor(.cavnarInk3))
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
            if let basis = card.basis, !basis.isEmpty {
                Text(basis).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    @ViewBuilder
    private func component(_ c: DSRScorecard.Component) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(c.label.uppercased()).font(.cavnarBody(10.5, weight: 700)).tracking(1.2)
                .foregroundStyle(Color.cavnarInk3)
            if c.measured, let value = c.value {
                if c.key == "guests" {
                    Text(value).font(.system(size: 17)).foregroundStyle(Color.cavnarEmber)
                        .accessibilityLabel(c.detail ?? value)
                } else {
                    HomeMixedText.make(value, size: 16, weight: 600,
                                       color: DSRScorecard.color(c.tone) ?? .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let detail = c.detail {
                    HomeMixedText.make(detail, size: 12, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text(c.why ?? "Not measured").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.cavnarInk3.opacity(0.07)))
    }
}

/// Today's wins and Today's risks, each a short checklist.
struct DSRWinsRisks: View {
    let card: DSRScorecard

    var body: some View {
        list("Today's wins", card.wins, glyph: "checkmark", tint: .cavnarGreen, empty: "Nothing stood out tonight.")
        list("Today's risks", card.risks, glyph: "exclamationmark.triangle.fill", tint: .cavnarAmber,
             empty: "Nothing to watch tonight.")
    }

    private func list(_ title: String, _ items: [DSRScorecard.Item], glyph: String, tint: Color,
                      empty: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title).font(.cavnarHeadline(19)).foregroundStyle(Color.cavnarInk)
            if items.isEmpty {
                Text(empty).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
            } else {
                ForEach(items) { item in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: glyph).font(.system(size: 13, weight: .bold)).foregroundStyle(tint)
                            .frame(width: 18).padding(.top, 2)
                            .accessibilityHidden(true)
                        HomeMixedText.make(item.text, size: 14.5, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}
