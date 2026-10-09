import SwiftUI

/// Home's one "More" group (iOS readability round, 10/8/26, #4): what is
/// not a decision for today — the recommendations, the measured results
/// (How you compare among them) — behind one closed row at the foot of the
/// page. "Decide, then more": the work above it, this below. The closed
/// row still carries data — the measured figure in its tone and one
/// sentence ("3 improved · $1,310/mo net measured") — so a glance gets the
/// result without opening it. Whether it was left open is remembered on
/// the device (@AppStorage).
struct HomeMoreDisclosure<Content: View>: View {
    let line: String
    /// The figure's colour — green for a measured gain, red for a net loss,
    /// ink3 when nothing is measured yet (never a green "$0").
    let tone: Color
    @ViewBuilder var content: () -> Content

    @AppStorage("home.more.open") private var isOpen = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                Haptic.light()
                if reduceMotion {
                    isOpen.toggle()
                } else {
                    withAnimation(.easeOut(duration: 0.22)) { isOpen.toggle() }
                }
            } label: {
                HStack(alignment: .center, spacing: CavnarSpace.s) {
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        Text("More")
                            .cavnarText(.headline)
                        Text("Recommendations, results, how you compare")
                            .cavnarText(.secondary)
                        HomeMixedText.make(line, role: .body, color: .cavnarInk, numberColor: tone)
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarSensitive()
                    }
                    Spacer(minLength: CavnarSpace.xs)
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.body))
                        .foregroundStyle(Color.cavnarInk2)
                        .rotationEffect(.degrees(isOpen ? 180 : 0))
                        .accessibilityHidden(true)
                }
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.horizontal, CavnarSpace.gutter)
            .accessibilityValue(isOpen ? "Expanded" : "Collapsed")
            .accessibilityHint(isOpen ? "Hides recommendations and results" : "Shows recommendations and results")

            if isOpen {
                content()
                    .padding(.top, CavnarSpace.m)
                    .transition(.opacity)
            }
        }
    }
}

/// Runs `action` once, the first time the view comes within a screen's
/// margin of the scroll view's visible area — Home's Results blocks read
/// their four endpoints then, not at launch (parity audit #20). A Home
/// VStack mounts every section at once, so `.onAppear` fires at launch;
/// this reads the view's place against the scroll view's own bounds.
private struct OnScrolledNear: ViewModifier {
    let margin: CGFloat
    let action: () -> Void
    @State private var fired = false

    func body(content: Content) -> some View {
        content.onGeometryChange(for: Bool.self) { proxy in
            // Outside a scroll view there is nothing to wait for.
            guard let visible = proxy.bounds(of: .scrollView) else { return true }
            return visible.insetBy(dx: 0, dy: -margin).intersects(CGRect(origin: .zero, size: proxy.size))
        } action: { near in
            guard near, !fired else { return }
            fired = true
            action()
        }
    }
}

extension View {
    func onScrolledNear(margin: CGFloat = 320, perform action: @escaping () -> Void) -> some View {
        modifier(OnScrolledNear(margin: margin, action: action))
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
