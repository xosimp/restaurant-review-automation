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
            HomeMixedText.make(label, size: CavnarType.caption, weight: 700, color: .cavnarInk2)
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
                .font(.cavnarNumber(CavnarType.caption, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .cavnarSensitive()
            HomeMixedText.make(OwnerCopy.displayLabel(pulse.label), size: CavnarType.caption, weight: 700, color: .cavnarInk2)
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

// MARK: - The fixed three-up glance (iOS readability round, 10/8/26, #94)

/// Home's glance under the hero: three fixed tiles — net last night (else
/// replies), labor %, rating — at a readable figure size, in place of the
/// horizontally scrolling pulse strip; and under them, data health as a
/// dot and words ("All data current", "1 source stale") rather than
/// "Data 87%". A tap on a tile opens its report or module; a tap on the
/// health line opens Data health. A missing measurement is "—", never 0.
struct HomeKPIRow: View {
    enum Target: Equatable {
        case report(String)
        case module(String)
    }

    struct Tile: Identifiable, Equatable {
        let id: String
        let value: String
        let label: String
        var detail: String? = nil
        var tone: Color = .cavnarInk
        let target: Target
        /// The tile's own tap-through, said ("Read the report") — the net
        /// tile carries last night's card before noon (iOS re-audit H3).
        var cta: String? = nil
    }

    struct Health: Equatable {
        let text: String
        let tone: Color
    }

    let tiles: [Tile]
    var health: Health? = nil
    var onOpen: (Target) -> Void
    var onOpenDataHealth: () -> Void = {}

    var body: some View {
        if !tiles.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                HStack(alignment: .top, spacing: CavnarSpace.xs) {
                    ForEach(tiles) { tile in
                        Button {
                            Haptic.light()
                            onOpen(tile.target)
                        } label: {
                            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                                Text(tile.value)
                                    .cavnarText(.figureM, color: tile.tone)
                                    .lineLimit(1)
                                    .minimumScaleFactor(0.85)
                                    .cavnarSensitive()
                                Text(tile.label)
                                    .cavnarText(.secondary)
                                    .lineLimit(1)
                                    .minimumScaleFactor(0.85)
                                if let detail = tile.detail {
                                    CavnarMixedText(detail, role: .caption, color: .cavnarInk2)
                                        .lineLimit(2)
                                }
                                if let cta = tile.cta {
                                    Text(cta)
                                        .cavnarText(.caption, color: .cavnarEmber2)
                                        .lineLimit(2)
                                }
                            }
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .topLeading)
                            .padding(CavnarSpace.s)
                            .background(Color.cavnarPaper2.opacity(0.85),
                                        in: RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                                .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityElement(children: .combine)
                        .accessibilityHint("Opens it")
                    }
                }
                if let health {
                    Button {
                        Haptic.light()
                        onOpenDataHealth()
                    } label: {
                        HStack(spacing: CavnarSpace.xs) {
                            Circle().fill(health.tone).frame(width: 8, height: 8)
                                .accessibilityHidden(true)
                            CavnarMixedText(health.text, role: .secondary)
                            Image(systemName: "chevron.right")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarInk2)
                                .accessibilityHidden(true)
                            Spacer(minLength: 0)
                        }
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Opens data health")
                }
            }
        }
    }

    /// The three tiles, from what Home already read: the latest report's
    /// net (Daily Sales Report — else the reviews reply count), the labor
    /// chip's figure and tone, and the newest week's rating.
    static func tiles(modules: [ModuleSummary], charts: HomeCharts?, night: DSRSummary?,
                      nightKicker: String?) -> [Tile] {
        var out: [Tile] = []
        var pulses: [String: ModulePulse] = [:]
        for m in modules where m.isAvailable {
            if let p = m.pulse, pulses[m.key] == nil { pulses[m.key] = p }
        }
        let replies: Tile? = pulses["reviews"].map { reviews in
            let read = Self.repliesFigure(reviews.value)
            return Tile(id: "reviews", value: read?.value ?? reviews.value, label: "Replied",
                        detail: read?.detail ?? OwnerCopy.displayLabel(reviews.label),
                        tone: Self.tone(reviews.tone), target: .module("reviews"))
        }
        if let night {
            out.append(Tile(id: "net", value: night.net.map { DSRFormat.money($0) } ?? "\u{2014}",
                            label: "Net, " + (nightKicker ?? "last night").lowercased(),
                            detail: night.vsYesterdayPct.map { DSRFormat.signedPct($0) + " vs the night before" },
                            tone: .cavnarInk, target: .report(night.businessDate), cta: "Read the report"))
        } else if let replies {
            out.append(replies)
        }
        if let labor = pulses["labor"] {
            let detail = labor.label.replacingOccurrences(of: "labor \u{00B7} ", with: "")
            out.append(Tile(id: "labor", value: labor.value, label: "Labor",
                            detail: detail.isEmpty ? nil : detail,
                            tone: Self.tone(labor.tone), target: .module("labor")))
        }
        if let week = charts?.rating.last(where: { $0.total > 0 }) {
            out.append(Tile(id: "rating", value: String(format: "%.1f\u{2605}", week.avg), label: "Rating",
                            detail: "\(week.total) review\(week.total == 1 ? "" : "s"), week of \(week.label)",
                            tone: .cavnarInk, target: .module("reviews")))
        } else if night != nil, let replies {
            out.append(replies)
        }
        return out
    }

    /// "45/52" → ("87%", "7 waiting") — the replies tile says the rate and
    /// what is left, not a fraction beside the same rate again (iOS
    /// re-audit L6). Nil for any other shape, which keeps the server's.
    static func repliesFigure(_ raw: String) -> (value: String, detail: String)? {
        let parts = raw.split(separator: "/").map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 2, let done = Int(parts[0]), let total = Int(parts[1]), total > 0,
              done >= 0, done <= total else { return nil }
        let pct = Int((Double(done) / Double(total) * 100).rounded())
        let waiting = total - done
        return ("\(pct)%", waiting == 0 ? "None waiting" : "\(waiting) waiting")
    }

    /// The figure's status colour: red over the named threshold, amber
    /// watch, green good, ink when there is no judgement to make (ember is
    /// never a status).
    static func tone(_ tone: String?) -> Color {
        switch tone {
        case "bad": return .cavnarRed
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        default: return .cavnarInk
        }
    }

    /// Data health in words: "Waiting for first sync", "1 source stale",
    /// "2 sources catching up", "All data current" — or "Couldn't check
    /// your data". Nil when the server sent nothing to state.
    static func healthLine(health: HomeDataHealth?, unavailable: Bool,
                           entries: [HomeFreshnessEntry]) -> Health? {
        if health?.overall?.isPending == true { return Health(text: "Waiting for first sync", tone: .cavnarInk3) }
        let stale = entries.filter { $0.state == .stale || $0.state == .disconnected }.count
        if stale > 0 { return Health(text: "\(stale) source\(stale == 1 ? "" : "s") stale", tone: .cavnarAmber) }
        let aging = entries.filter { $0.state == .aging }.count
        if aging > 0 {
            return Health(text: "\(aging) source\(aging == 1 ? "" : "s") catching up", tone: .cavnarAmber)
        }
        if entries.contains(where: { $0.state == .current }) {
            return Health(text: "All data current", tone: .cavnarGreen)
        }
        if let pct = health?.overall?.pct {
            return pct >= 90 ? Health(text: "All data current", tone: .cavnarGreen)
                             : Health(text: "Some data is behind", tone: .cavnarAmber)
        }
        if unavailable { return Health(text: "Couldn\u{2019}t check your data", tone: .cavnarAmber) }
        return nil
    }
}
