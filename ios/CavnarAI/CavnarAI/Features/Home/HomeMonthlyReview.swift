import SwiftUI

/// Last month, read the way an owner reads a P&L — the retention audit's
/// #13, which shipped as an email on the 1st and then as a web card. The
/// same build as both (`monthly_review.build` behind /mobile/api/monthly-
/// review), so the phone, the screen and the email agree to the digit.
///
/// Each metric is set against the month before and, once thirteen months
/// exist, the same month last year — a month-over-month read cannot tell a
/// seasonal dip from a slide. A metric with no value is left out rather
/// than shown as zero, and a month with nothing measured hides the card.
struct HomeMonthlyReview: Decodable {
    struct Metric: Decodable, Identifiable {
        let key: String
        let label: String
        let unit: String?
        let value: Double?
        let previous: Double?
        let delta: Double?
        let verdict: String?
        let why: String?
        let ask: String?
        var id: String { key }
    }
    /// One tracker row (outcomes.list_outcomes). `counts` is authoritative
    /// for its colour (RecOutcome.standing); an older row without it falls
    /// back to the verdict.
    struct Result: Decodable, Identifiable {
        let id: Int?
        let title: String?
        let summary: String?
        let verdict: String?
        let counts: Bool?

        enum CodingKeys: String, CodingKey { case id, title, summary, verdict, counts }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try? c.decodeIfPresent(Int.self, forKey: .id)
            title = try? c.decodeIfPresent(String.self, forKey: .title)
            summary = try? c.decodeIfPresent(String.self, forKey: .summary)
            verdict = try? c.decodeIfPresent(String.self, forKey: .verdict)
            counts = try? c.decodeIfPresent(Bool.self, forKey: .counts)
        }

        var standing: RecOutcome.Standing { RecOutcome.standing(verdict: verdict, counts: counts) }
    }
    struct Priority: Decodable {
        let label: String
        let monthly: Double?
        let isRange: Bool?
        let monthlyLow: Double?
        let monthlyHigh: Double?
        enum CodingKeys: String, CodingKey {
            case label, monthly
            case isRange = "is_range"
            case monthlyLow = "monthly_low"
            case monthlyHigh = "monthly_high"
        }
    }
    struct Review: Decodable {
        let month: String
        let comparedWith: String?
        let metrics: [Metric]
        let results: [Result]?
        let priorities: [Priority]?
        let months: Int?
        enum CodingKeys: String, CodingKey {
            case month, metrics, results, priorities, months
            case comparedWith = "compared_with"
        }
    }
    let ok: Bool
    let review: Review?
    let headline: String?
    /// "Walk me through August 2026" — the card's own question.
    let ask: String?
    /// Per metric key, the " — 1.2 points under the same month last year"
    /// clause, or "" — the server only writes one when last year could be
    /// measured and the move clears the noise band.
    let yoy: [String: String]?

    var measured: [Metric] { (review?.metrics ?? []).filter { $0.value != nil } }
    var hasAnything: Bool { !measured.isEmpty || !(review?.results ?? []).isEmpty }
}

struct HomeMonthlyReviewCard: View {
    let month: HomeMonthlyReview

    var body: some View {
        if let r = month.review, month.hasAnything {
            VStack(alignment: .leading, spacing: 10) {
                HomeSectionHeader(kicker: (r.months ?? 1) > 1 ? "Last quarter" : "Last month",
                                  title: r.month,
                                  trailing: r.comparedWith.map { "against \($0)" })
                VStack(alignment: .leading, spacing: 14) {
                    if let head = month.headline, !head.isEmpty {
                        HomeMixedText.make(head, size: 17, weight: 700, color: .cavnarInk)
                    }
                    if !month.measured.isEmpty {
                        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)],
                                  alignment: .leading, spacing: 10) {
                            ForEach(month.measured) { m in
                                tile(m, comparedWith: r.comparedWith)
                            }
                        }
                    }
                    let results = (r.results ?? []).filter { ($0.summary ?? $0.title) != nil }.prefix(3)
                    if !results.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            kicker("What your changes did")
                            ForEach(Array(results.enumerated()), id: \.offset) { _, res in
                                HStack(alignment: .top, spacing: 10) {
                                    Circle().fill(Self.tone(res.standing)).frame(width: 7, height: 7).padding(.top, 6)
                                    HomeMixedText.make(res.summary ?? res.title ?? "", size: 13.5, weight: 500, color: .cavnarInk2)
                                }
                            }
                        }
                    }
                    HomeAskLink(question: month.ask ?? "Walk me through \(r.month)",
                                label: "Ask about this month")
                    let priorities = (r.priorities ?? []).prefix(3)
                    if !priorities.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            kicker("Worth your time next month")
                            ForEach(Array(priorities.enumerated()), id: \.offset) { _, p in
                                HomeMixedText.make(Self.priorityLine(p), size: 13.5, weight: 500, color: .cavnarInk2)
                            }
                        }
                    }
                }
                .cavnarCard()
            }
        }
    }

    private func kicker(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(11, weight: 700))
            .tracking(1.4)
            .foregroundStyle(Color.cavnarInk3)
            .padding(.top, 2)
    }

    private func tile(_ m: HomeMonthlyReview.Metric, comparedWith: String?) -> some View {
        let tone = Self.tone(m.verdict)
        return VStack(alignment: .leading, spacing: 4) {
            Text(m.label.uppercased())
                .font(.cavnarBody(10.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarInk3)
                .lineLimit(1)
            Text(Self.format(m.value ?? 0, unit: m.unit))
                .font(.cavnarNumber(22, weight: 700))
                .foregroundStyle(tone == Color.cavnarInk3 ? Color.cavnarInk : tone)
            if let delta = m.delta, m.previous != nil, let cw = comparedWith {
                HomeMixedText.make(Self.formatDelta(delta, unit: m.unit) + " vs " + cw,
                                   size: 12, weight: 500, color: .cavnarInk3)
            } else if let why = m.why, !why.isEmpty {
                Text(why).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let yc = month.yoy?[m.key], !yc.isEmpty {
                HomeMixedText.make(yc.replacingOccurrences(of: "^ — ", with: "", options: .regularExpression),
                                   size: 12, weight: 500, color: .cavnarInk3)
            }
            if let ask = m.ask {
                HomeAskLink(question: ask, label: "Ask").padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color.cavnarPaper2.opacity(0.6), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    static func tone(_ verdict: String?) -> Color {
        switch verdict {
        case "improved": return .cavnarGreen
        case "worsened": return .cavnarEmber
        default: return .cavnarInk3
        }
    }

    /// A result row's colour: only a result that counts is green or ember.
    static func tone(_ standing: RecOutcome.Standing) -> Color {
        switch standing {
        case .good: return .cavnarGreen
        case .bad: return .cavnarEmber
        case .neutral: return .cavnarInk3
        }
    }

    static func format(_ v: Double, unit: String?) -> String {
        switch unit {
        case "$": return "$" + Self.grouped(v)
        case "%": return String(format: "%.1f%%", v)
        case "★": return String(format: "%.2f★", v)
        default: return Self.grouped(v)
        }
    }

    static func formatDelta(_ d: Double, unit: String?) -> String {
        let sign = d > 0 ? "+" : ""
        switch unit {
        case "$": return sign + "$" + Self.grouped(d)
        case "%": return sign + String(format: "%.1f pts", d)
        case "★": return sign + String(format: "%.2f", d)
        default: return sign + Self.grouped(d)
        }
    }

    static func priorityLine(_ p: HomeMonthlyReview.Priority) -> String {
        if p.isRange == true, let lo = p.monthlyLow, let hi = p.monthlyHigh {
            return "\(p.label) · $\(grouped(lo))–$\(grouped(hi))/mo"
        }
        if let m = p.monthly, m > 0 { return "\(p.label) · $\(grouped(m))/mo" }
        return p.label
    }

    private static func grouped(_ v: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        return f.string(from: NSNumber(value: v.rounded())) ?? "\(Int(v.rounded()))"
    }
}
