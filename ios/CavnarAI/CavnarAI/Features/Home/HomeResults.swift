import SwiftUI

/// Home's one "Results" section (DESIGN_SYSTEM.md §11b step 7, density #4):
/// everything measured — the value band, How you compare, what the
/// owner's changes did, what worked, this month — behind a single
/// disclosure that starts closed. Its closed row still carries data: the
/// measured figure at body scale and one sentence ("3 improved · 1 got
/// worse · $1,310/mo net measured"), so a glance gets the result without
/// the section taking the top of the page from the work. Whether it was
/// left open is remembered on the device (@AppStorage).
struct HomeResultsDisclosure<Content: View>: View {
    let line: String
    /// The figure's colour — green for a measured gain, red for a net loss,
    /// ink3 when nothing is measured yet (never a green "$0").
    let tone: Color
    @ViewBuilder var content: () -> Content

    @AppStorage("home.results.open") private var isOpen = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.22)) { isOpen.toggle() }
            } label: {
                HStack(alignment: .center, spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("RESULTS")
                            .font(.cavnarBody(CavnarType.kicker, weight: 700))
                            .tracking(1.6)
                            .foregroundStyle(Color.cavnarEmber2)
                        HomeMixedText.make(line, size: CavnarType.body, weight: 600,
                                           color: .cavnarInk, numberColor: tone)
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarSensitive()
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(Color.cavnarInk3)
                        .rotationEffect(.degrees(isOpen ? 180 : 0))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 20)
            .accessibilityHint(isOpen ? "Hides the measured results" : "Shows the measured results")

            if isOpen {
                content()
                    .padding(.top, 16)
                    .transition(.opacity)
            }
        }
    }
}

/// The closed row's sentence, pure so the rule is pinned by tests. Only
/// the measured figure is ever named — never an opportunity or an
/// estimate (CLAUDE.md, value rule) — and "net" only when something
/// measured got worse.
enum HomeResultsSummary {
    static func line(headline: HomeValueHeadline, improved: Int?, worse: Int?) -> String {
        var parts: [String] = []
        if let improved, improved > 0 { parts.append("\(improved) improved") }
        if let worse, worse > 0 { parts.append("\(worse) got worse") }
        let figure = headline.figure
        if headline.isNet {
            parts.append("\(money(figure))/mo net measured")
        } else if figure > 0 {
            parts.append("\(money(figure))/mo measured")
        }
        return parts.isEmpty ? "Nothing measured yet" : parts.joined(separator: " \u{00B7} ")
    }

    static func tone(headline: HomeValueHeadline) -> Color {
        if headline.figure < 0 { return .cavnarRed }
        if headline.figure == 0 && !headline.isNet { return .cavnarInk3 }
        return .cavnarGreen
    }

    private static func money(_ v: Int) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        let s = f.string(from: NSNumber(value: abs(v))) ?? "\(abs(v))"
        return (v < 0 ? "\u{2212}$" : "$") + s
    }
}
