import SwiftUI

/// The pulse strip — one breathing chip per active module, built from the
/// same KPI the Modules tile shows: the number, a short label, and a dot
/// whose colour is the only semantic colour on the fold (green good, amber
/// warn, ember otherwise). The strip drifts right to left on its own, on a
/// seamless loop, like a ticker — chips stay tappable while it moves.
struct HomePulseStrip: View {
    let modules: [ModuleSummary]
    let onSelect: (ModuleSummary) -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    // Measured once from a single, undoubled copy of the row — see
    // MarqueeWidthKey below. The loop can't start until this is known, so
    // the marquee sits still (not jumping in from some guessed width) on
    // its very first frame.
    @State private var unitWidth: CGFloat = 0
    @State private var start = Date()

    private static let gap: CGFloat = 8
    // Points per second the strip drifts — slow enough to actually read
    // each chip, not a stock-ticker sprint.
    private static let speed: CGFloat = 16

    private var chips: [ModuleSummary] {
        modules.filter { $0.isAvailable && $0.pulse != nil }
    }

    var body: some View {
        if !chips.isEmpty {
            if reduceMotion {
                // Reduce Motion: the automatic drift is exactly the kind of
                // ambient motion that setting asks to stop — same rule
                // CavnarMotion's continuous loops already follow. A plain,
                // manually-scrollable row keeps every chip reachable.
                ScrollView(.horizontal, showsIndicators: false) {
                    chipRow(chips)
                        .padding(.horizontal, 20)
                        .padding(.vertical, 6)
                }
                .scrollClipDisabled()
            } else {
                marquee
            }
        }
    }

    private var marquee: some View {
        // Wall-clock driven (TimelineView), not a toggled repeatForever —
        // the app's other continuous loops (PulsingSwipeArrow, the obsidian
        // field's embers) all learned the same lesson: a phase animation's
        // transaction can be interrupted by a tab switch and never resume,
        // leaving the strip frozen. A position computed from real elapsed
        // time can't get stuck on any frame it's asked to render.
        TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { timeline in
            let period = Double(unitWidth + Self.gap)
            let elapsed = timeline.date.timeIntervalSince(start)
            let shift: CGFloat = period > 0
                ? CGFloat((elapsed * Double(Self.speed)).truncatingRemainder(dividingBy: period))
                : 0
            HStack(spacing: Self.gap) {
                // Three back-to-back copies of the same set: as the first
                // scrolls fully off the left edge the second is already in
                // its place, and the third covers a screen wide enough that
                // even one or two chips never leave a visible gap. Only the
                // first copy reports its width — all three are identical.
                chipRow(chips)
                    .background(
                        GeometryReader { geo in
                            Color.clear.preference(key: MarqueeWidthKey.self, value: geo.size.width)
                        }
                    )
                chipRow(chips)
                chipRow(chips)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 6)
            .offset(x: unitWidth > 0 ? -shift : 0)
        }
        .frame(height: 42)
        .clipped()
        .onPreferenceChange(MarqueeWidthKey.self) { unitWidth = $0 }
    }

    private func chipRow(_ items: [ModuleSummary]) -> some View {
        HStack(spacing: Self.gap) {
            ForEach(items) { module in
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
        .fixedSize()
    }
}

private struct MarqueeWidthKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        // Every reporting copy is identical width — last-write-wins is fine.
        value = nextValue()
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
        .fixedSize()
    }
}
