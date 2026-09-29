import SwiftUI

/// "Keep suggesting trim day?" — the kinds Cavnar AI stopped suggesting
/// because this restaurant's own measured results say they did no better
/// than doing nothing (memory round 9/29/26, M4 "thresholds";
/// home_brief.kind_holds). Each is a question, answered where it is read
/// with the ledger's own two answers in the server's words: Done ("Keep
/// suggesting it") keeps the kind, Not for us ("Stop suggesting it")
/// leaves it stopped. Under the why, the record it rests on as two bars —
/// how often it improved here against how often doing nothing would have —
/// so the owner sees the gap, not only reads it. Draws nothing without a
/// hold.
struct HomeKindHolds: View {
    let holds: [HomeKindHold]

    var body: some View {
        if !holds.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                HomeSectionHeader(kicker: "Your record says", title: "Keep suggesting these?",
                                  trailing: holds.count > 1 ? "\(holds.count)" : nil)
                VStack(spacing: 0) {
                    ForEach(Array(holds.enumerated()), id: \.element.id) { index, hold in
                        HomeKindHoldRow(hold: hold)
                            .padding(.vertical, 12)
                        if index < holds.count - 1 { AccountRowDivider() }
                    }
                }
                .cavnarCard(.ai)
            }
        }
    }
}

struct HomeKindHoldRow: View {
    let hold: HomeKindHold

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HomeMixedText.make(hold.title, size: CavnarType.body, weight: 700, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            if let why = hold.why {
                HomeMixedText.make(why, size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let bars = HomeKindHoldBars.Model(hold) {
                HomeKindHoldBars(model: bars)
                    .padding(.vertical, 2)
            }
            if hold.showsAnswers {
                RecAnswerRow(key: hold.answerKey, surface: "home", module: "home",
                             answers: [.completed, .notForUs],
                             labels: [.completed: hold.keepLabel, .notForUs: hold.stopLabel],
                             asksReason: false)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Two bars on one scale: "Improved here" (this kind's own record) and "By
/// doing nothing" (the chance it would have improved anyway). The kind's
/// bar is amber — it is why the kind is held — and doing nothing is ink3.
/// Figures in the number face; the bars grow in once, no bounce.
struct HomeKindHoldBars: View {
    struct Model: Equatable {
        let improvedPct: Int
        let improved: Int
        let measured: Int
        let doNothingPct: Int

        /// Nil unless the server sent both sides of the comparison.
        init?(_ hold: HomeKindHold) {
            guard let m = hold.measured, m > 0, let d = hold.doNothingPct else { return nil }
            let i = max(0, min(hold.improved ?? 0, m))
            improved = i
            measured = m
            improvedPct = Int((Double(i) / Double(m) * 100).rounded())
            doNothingPct = max(0, min(d, 100))
        }
    }

    let model: Model
    @State private var grown = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            bar(label: "Improved here", figure: "\(model.improved) of \(model.measured)",
                pct: model.improvedPct, fill: LinearGradient(colors: [.cavnarAmber.opacity(0.65), .cavnarAmber],
                                                           startPoint: .leading, endPoint: .trailing),
                glow: .cavnarAmber)
            bar(label: "By doing nothing", figure: "\(model.doNothingPct)%",
                pct: model.doNothingPct, fill: LinearGradient(colors: [.cavnarInk3.opacity(0.45), .cavnarInk3],
                                                            startPoint: .leading, endPoint: .trailing),
                glow: .clear)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Improved \(model.improved) of \(model.measured) times here, \(model.improvedPct) percent. "
                            + "Doing nothing would have improved \(model.doNothingPct) percent of the time.")
        .onAppear {
            if reduceMotion { grown = true } else {
                withAnimation(.easeOut(duration: 0.6).delay(0.1)) { grown = true }
            }
        }
    }

    private func bar(label: String, figure: String, pct: Int, fill: LinearGradient, glow: Color) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Text(label.uppercased())
                    .font(.cavnarBody(CavnarType.kicker - 1, weight: 700))
                    .tracking(1.0)
                    .foregroundStyle(Color.cavnarInk3)
                Spacer(minLength: 8)
                HomeMixedText.make(figure, size: CavnarType.caption, weight: 700, color: .cavnarInk2,
                                   numberWeight: 700, numberColor: .cavnarInk)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                    Capsule().fill(fill)
                        .frame(width: max(4, geo.size.width * CGFloat(grown ? pct : 0) / 100))
                        .shadow(color: glow.opacity(0.45), radius: 6)
                }
            }
            .frame(height: 6)
        }
    }
}
