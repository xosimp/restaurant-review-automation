import SwiftUI

/// The Account hero's health card (iOS parity #89): the server's account
/// health ring, its one sentence and one Fix link (iOS readability round;
/// the plan's modules and the measured-value line are on Billing) —
/// `delivered` only, never an opportunity figure (CLAUDE.md "Value
/// delivered"). Everything here is account_health.payload as the web's
/// Account overview reads it; nothing is scored on the phone.
struct AccountHealthCard: View {
    let health: AccountHealth
    /// The Fix link's action, by the item it fixes (profile, people,
    /// integrations, notifications, security, subscription).
    var onFix: (String) -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var drawn = false

    private var tone: Color { Self.toneColor(health.tone) }

    static func toneColor(_ tone: String) -> Color {
        switch tone {
        case "good", "ok": return .cavnarGreen
        case "bad": return .cavnarRed
        default: return .cavnarAmber
        }
    }

    var body: some View {
        // Ring + one sentence + Fix (iOS readability round). The six checked
        // items, the plan's modules and the measured-value line left the
        // card: the modules and the value line are on the Billing sheet,
        // and Fix opens the one thing worth doing.
        HStack(alignment: .center, spacing: CavnarSpace.m) {
            ring
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Account health")
                if let sentence = health.sayLead ?? health.sayText {
                    CavnarMixedText(sentence, role: .body, color: .cavnarInk)
                        .lineLimit(3)
                }
                if let fix = health.fix {
                    Button {
                        Haptic.light()
                        onFix(fix.key)
                    } label: {
                        HStack(spacing: 6) {
                            Text(fix.label).cavnarText(.label, color: .cavnarEmber2)
                            Image(systemName: "chevron.right")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarEmber2)
                                .accessibilityHidden(true)
                        }
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Opens the setting that fixes it")
                }
            }
            Spacer(minLength: 0)
        }
        .padding(CavnarSpace.m)
        .background(
            LinearGradient(colors: [Color.cavnarPaper2, tone.opacity(0.06)], startPoint: .topLeading, endPoint: .bottomTrailing)
        )
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(tone.opacity(0.28), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .onAppear {
            guard !reduceMotion else { drawn = true; return }
            withAnimation(.timingCurve(0.2, 0.9, 0.3, 1, duration: 1.0)) { drawn = true }
        }
    }

    private var ring: some View {
        let fraction = CGFloat(min(max(health.score ?? 0, 0), 100)) / 100
        return ZStack {
            Circle().stroke(Color.cavnarInk3.opacity(0.18), lineWidth: 7)
            Circle()
                .trim(from: 0, to: drawn ? fraction : 0)
                .stroke(
                    AngularGradient(colors: [tone.opacity(0.55), tone, tone], center: .center,
                                    startAngle: .degrees(0), endAngle: .degrees(360 * Double(max(fraction, 0.01)))),
                    style: StrokeStyle(lineWidth: 7, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
                .shadow(color: tone.opacity(0.55), radius: 6)
            VStack(spacing: 0) {
                Text(health.score.map(String.init) ?? "\u{2014}")
                    .cavnarText(.figureM)
                    .minimumScaleFactor(0.85)
                Text("set up")
                    .cavnarText(.caption)
            }
        }
        .frame(width: 84, height: 84)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(health.score.map { "Account setup score \($0) of 100" } ?? "Account setup score not known yet")
    }
}

/// The modules on the plan and the measured-value line — the health card's
/// other two parts, shown on the Billing sheet (iOS readability round).
/// `delivered` only, never an opportunity figure (CLAUDE.md "Value
/// delivered"); "Nothing measured yet" says so, never $0.
struct AccountPlanModules: View {
    let health: AccountHealth

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            if !health.features.isEmpty {
                AccountFlowLayout(spacing: 6) {
                    ForEach(health.features) { f in
                        AccountChip(text: f.on ? f.label : "\(f.label) · not on plan", muted: !f.on)
                            .accessibilityLabel(f.on ? "\(f.label): on your plan. \(f.detail)" : "\(f.label): not on your plan")
                    }
                }
            }
            if let line = health.measured?.line {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    Image(systemName: "chart.line.uptrend.xyaxis")
                        .font(.cavnar(.caption))
                        .foregroundStyle(health.measured?.measured == true ? Color.cavnarGreen : Color.cavnarInk3)
                        .accessibilityHidden(true)
                    CavnarMixedText(line, role: .secondary)
                }
            }
        }
    }
}
