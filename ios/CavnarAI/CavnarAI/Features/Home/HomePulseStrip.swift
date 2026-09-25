import SwiftUI

/// The pulse strip — one breathing chip per active module, built from the
/// same KPI the Modules tile shows: the number, a short label, and a dot
/// whose colour is the only semantic colour on the fold (green good, amber
/// warn, ember otherwise). A plain horizontally-scrollable row — an earlier
/// pass tried an auto-scrolling marquee here (three duplicated copies of
/// every chip), which is what caused the Home layout regression this
/// session traced live on device: the duplicated content's true width, with
/// nothing capping it, leaked straight up through the layout tree and every
/// other section on the page inherited it. A small nudge arrow after the
/// last chip (the PulsingSwipeArrow motion) is the
/// hint that there's more to see, instead.
struct HomePulseStrip: View {
    let modules: [ModuleSummary]
    // Set true while a sheet is presented over Home — see
    // HomeObsidianField's own doc comment on `paused` for why: each chip's
    // breathing dot is its own always-on TimelineView, and left running
    // they kept costing frames during a sheet's transition and interactive
    // dismiss even while fully covered.
    var paused: Bool = false
    /// Modules whose data reads stale or disconnected (freshness[]): their
    /// chip carries an amber clock (density #21) — the freshness strip that
    /// used to sit under this row is folded in here.
    var staleModules: Set<String> = []
    /// The Data health chip at the row's end ("Data 92%"), and what a tap
    /// on it opens. Nil draws no chip.
    var dataChip: String? = nil
    var onOpenDataHealth: (() -> Void)? = nil
    let onSelect: (ModuleSummary) -> Void

    /// Which modules' sources read stale or disconnected — only what the
    /// server marked so; aging, unknown and sample are not "stale".
    static func staleModules(_ entries: [HomeFreshnessEntry]) -> Set<String> {
        Set(entries.filter { $0.state == .stale || $0.state == .disconnected }.compactMap(\.module))
    }

    /// "Data 92%", "Waiting for first sync", or "Data: couldn't check" —
    /// the freshness strip's kicker at chip size. Nil when the server sent
    /// nothing to state.
    static func dataChipLabel(health: HomeDataHealth?, unavailable: Bool) -> String? {
        if health?.overall?.isPending == true { return "Waiting for first sync" }
        if let pct = health?.overall?.pct { return "Data \(pct)%" }
        if unavailable { return "Data: couldn\u{2019}t check" }
        return nil
    }

    private var chips: [ModuleSummary] {
        modules.filter { $0.isAvailable && $0.pulse != nil }
    }

    var body: some View {
        if !chips.isEmpty {
            VStack(spacing: 4) {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(chips) { module in
                            if let pulse = module.pulse {
                                Button {
                                    onSelect(module)
                                } label: {
                                    PulseChip(pulse: pulse, paused: paused,
                                              stale: staleModules.contains(module.key))
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel("\(module.label): \(pulse.value) \(OwnerCopy.displayLabel(pulse.label))"
                                                    + (staleModules.contains(module.key) ? ", data is stale" : ""))
                            }
                        }
                        if let dataChip {
                            Button {
                                Haptic.light()
                                onOpenDataHealth?()
                            } label: {
                                DataHealthChip(label: dataChip, stale: !staleModules.isEmpty)
                            }
                            .buttonStyle(.plain)
                            .disabled(onOpenDataHealth == nil)
                            .accessibilityHint("Opens data health")
                        }
                    }
                    .padding(.horizontal, 20)
                    .padding(.vertical, 6)
                }
                // The chips carry a soft glow past their own bounds — let it
                // show instead of clipping it at the scroll view's edge.
                .scrollClipDisabled()

                // Underneath the row, not inside it. As the last item in the
                // HStack this sat at the far right END of the scroll
                // content — i.e. only visible once you had already scrolled
                // all the way over, which is precisely when the hint is no
                // longer of any use. Below the strip it is on screen from
                // the start, which is the entire point of a swipe hint.
                if chips.count > 1 {
                    PulsingSwipeArrow(size: 13)
                        .accessibilityHidden(true)
                }
            }
        }
    }
}

extension HomePulseStrip {
    /// The server's tones (mobile_api._home_pulse): "bad" red — a named
    /// threshold crossed (density #31) — "warn" amber, "good" green, and
    /// nil ember (no judgement to make).
    static func toneColor(_ tone: String?) -> Color {
        switch tone {
        case "bad": return .cavnarRed
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        default: return .cavnarEmber2
        }
    }
}

/// The row's last chip: the Data Health Score, amber-edged when any
/// module's data is stale. A tap opens the Data health sheet.
private struct DataHealthChip: View {
    let label: String
    var stale: Bool = false

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: stale ? "clock.badge.exclamationmark" : "waveform.path.ecg")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(stale ? Color.cavnarAmber : Color.cavnarInk3)
                .accessibilityHidden(true)
            HomeMixedText.make(label, size: 12, weight: 700, color: .cavnarInk2)
                .lineLimit(1)
            Image(systemName: "chevron.right")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(Color.cavnarInk3)
                .accessibilityHidden(true)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(Capsule().fill(Color.cavnarPaper2.opacity(0.7)))
        .overlay(Capsule().strokeBorder(stale ? Color.cavnarAmber.opacity(0.6) : Color.cavnarPaper3.opacity(0.8),
                                        lineWidth: 1))
    }
}

private struct PulseChip: View {
    let pulse: ModulePulse
    var paused: Bool = false
    var stale: Bool = false

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var start = Date()

    private var dotColor: Color {
        HomePulseStrip.toneColor(pulse.tone)
    }

    var body: some View {
        HStack(spacing: 8) {
            // One slow breath every 2.2s — wall-clock driven so it can't
            // stall the way a toggled repeatForever can after a tab swap.
            TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion || paused)) { timeline in
                let t = timeline.date.timeIntervalSince(start)
                let phase = (reduceMotion || paused) ? 0.5 : 0.5 - 0.5 * cos(t * 2 * .pi / 2.2)
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
            HomeMixedText.make(OwnerCopy.displayLabel(pulse.label), size: 12, weight: 700, color: .cavnarInk2)
                .lineLimit(1)
            if stale {
                Image(systemName: "clock.badge.exclamationmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.cavnarAmber)
                    .accessibilityHidden(true)
            }
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
