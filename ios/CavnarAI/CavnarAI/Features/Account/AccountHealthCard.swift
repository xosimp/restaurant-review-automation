import SwiftUI

/// The Account hero's health card (iOS parity #89): the server's account
/// health ring and its one sentence with one Fix link, the six items behind
/// a disclosure, the modules on the plan, and the measured-value line —
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
    @State private var showingItems = false

    private var tone: Color { Self.toneColor(health.tone) }

    static func toneColor(_ tone: String) -> Color {
        switch tone {
        case "good", "ok": return .cavnarGreen
        case "bad": return .cavnarRed
        default: return .cavnarAmber
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .center, spacing: 16) {
                ring
                VStack(alignment: .leading, spacing: 6) {
                    Text("ACCOUNT HEALTH")
                        .font(.cavnarBody(12.5, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                    if let lead = health.sayLead {
                        HomeMixedText.make(lead, size: 16, weight: 700, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let text = health.sayText {
                        HomeMixedText.make(text, size: 14, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                            .lineLimit(4)
                    }
                    if let fix = health.fix {
                        Button {
                            Haptic.light()
                            onFix(fix.key)
                        } label: {
                            HStack(spacing: 6) {
                                Text(fix.label).font(.cavnarBody(14.5, weight: 700))
                                Image(systemName: "chevron.right").font(.system(size: 11, weight: .bold))
                            }
                            .foregroundStyle(Color.cavnarEmber)
                            .frame(minHeight: 36)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint("Opens the setting that fixes it")
                    }
                }
                Spacer(minLength: 0)
            }

            // The six items, worst first, behind one tap (progressive disclosure).
            if !health.items.isEmpty {
                Button {
                    Haptic.selection()
                    withAnimation(.easeOut(duration: 0.22)) { showingItems.toggle() }
                } label: {
                    HStack {
                        Text(showingItems ? "Hide what's checked" : "What's checked")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarInk2)
                        Spacer()
                        Image(systemName: showingItems ? "chevron.up" : "chevron.down")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    .frame(minHeight: 36)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if showingItems {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(sortedItems) { item in
                            Button {
                                Haptic.light()
                                onFix(item.key)
                            } label: {
                                HStack(alignment: .top, spacing: 10) {
                                    Circle().fill(Self.toneColor(item.state))
                                        .frame(width: 8, height: 8)
                                        .shadow(color: Self.toneColor(item.state).opacity(0.6), radius: 3)
                                        .padding(.top, 6)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(item.label).font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                                        HomeMixedText.make(item.sub, size: 13.5, weight: 500, color: .cavnarInk3)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                    Spacer(minLength: 0)
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }

            if !health.features.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("ON YOUR PLAN")
                        .font(.cavnarBody(12.5, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                    AccountFlowLayout(spacing: 6) {
                        ForEach(health.features) { f in
                            AccountChip(text: f.on ? f.label : "\(f.label) · not on plan", muted: !f.on)
                                .accessibilityLabel(f.on ? "\(f.label): on your plan. \(f.detail)" : "\(f.label): not on your plan")
                        }
                    }
                }
            }

            // Measured value: delivered, net of what got worse — the only
            // figure shown here. "Nothing measured yet" says so; never $0.
            if let line = health.measured?.line {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "chart.line.uptrend.xyaxis")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(health.measured?.measured == true ? Color.cavnarGreen : Color.cavnarInk3)
                    HomeMixedText.make(line, size: 14, weight: 600,
                                       color: health.measured?.measured == true ? .cavnarInk2 : .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(16)
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

    /// Worst first: what needs you, then what's worth setting up, then done.
    private var sortedItems: [AccountHealth.Item] {
        let rank = ["bad": 0, "warn": 1, "ok": 2]
        return health.items.enumerated().sorted {
            (rank[$0.element.state] ?? 1, $0.offset) < (rank[$1.element.state] ?? 1, $1.offset)
        }.map(\.element)
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
                    .font(.cavnarNumber(24, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text("set up")
                    .font(.cavnarBody(11, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(width: 78, height: 78)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(health.score.map { "Account setup score \($0) of 100" } ?? "Account setup score not known yet")
    }
}
