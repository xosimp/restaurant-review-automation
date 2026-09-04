import SwiftUI

/// The pulse strip — one breathing chip per active module, built from the
/// same KPI the Modules tile shows: the number, a short label, and a dot
/// whose colour is the only semantic colour on the fold (green good, amber
/// warn, ember otherwise). Tapping a chip opens that module.
struct HomePulseStrip: View {
    let modules: [ModuleSummary]
    let onSelect: (ModuleSummary) -> Void

    private var chips: [ModuleSummary] {
        modules.filter { $0.isAvailable && $0.pulse != nil }
    }

    var body: some View {
        if !chips.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(chips) { module in
                        if let pulse = module.pulse {
                            Button {
                                onSelect(module)
                            } label: {
                                PulseChip(pulse: pulse)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("\(module.label): \(pulse.value) \(pulse.label)")
                        }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 6)
            }
            // The chips carry a soft glow past their own bounds — let it
            // show instead of clipping it at the scroll view's edge.
            .scrollClipDisabled()
        }
    }
}

private struct PulseChip: View {
    let pulse: ModulePulse

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var start = Date()

    private var dotColor: Color {
        switch pulse.tone {
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        default: return .cavnarEmber2
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            // One slow breath every 2.2s — wall-clock driven so it can't
            // stall the way a toggled repeatForever can after a tab swap.
            TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
                let t = timeline.date.timeIntervalSince(start)
                let phase = reduceMotion ? 0.5 : 0.5 - 0.5 * cos(t * 2 * .pi / 2.2)
                Circle()
                    .fill(dotColor)
                    .frame(width: 6, height: 6)
                    .scaleEffect(1 + 0.35 * phase)
                    .opacity(0.85 + 0.15 * phase)
                    .shadow(color: dotColor.opacity(0.9), radius: 4)
            }
            .frame(width: 10, height: 10)

            Text(pulse.value)
                .font(.cavnarNumber(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .cavnarSensitive()
            HomeMixedText.make(pulse.label, size: 12, weight: 700, color: .cavnarInk2)
                .lineLimit(1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(
            Capsule().fill(
                LinearGradient(colors: [Color.white.opacity(0.06), Color.white.opacity(0.02)],
                               startPoint: .top, endPoint: .bottom)
            )
        )
        .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.28), lineWidth: 1))
        .shadow(color: Color.cavnarEmber.opacity(0.14), radius: 9, x: 0, y: 0)
    }
}
