import SwiftUI

// The pieces the Daily Report screen is built from. Each one is documented
// in DESIGN_SYSTEM.md §12 (iOS) under "Daily report".

// MARK: - Status

/// Final (green) / Missing data (amber) / Running (ember, breathing) /
/// Couldn't finish (red) — the night's state, in words, never colour alone.
/// The report draws it only when the night is not Final (10/8/26): a
/// finished night needs no badge.
struct DSRStatusPill: View {
    let phase: DSRPhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: CavnarSpace.xxs + 1) {
            if phase == .running {
                BreathingDot(color: .cavnarEmber, paused: reduceMotion)
                    .frame(width: 12, height: 12)
            }
            Text(phase.label)
                .font(.cavnarBody(CavnarType.caption, weight: 700))
        }
        .foregroundStyle(tone.foreground)
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .background(tone.background)
        .clipShape(Capsule())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Status: \(phase.label)")
    }

    private var tone: CavnarTone {
        switch phase {
        case .final: return .good
        case .provisional: return .warning
        case .failed: return .bad
        case .running, .notStarted: return .neutral
        }
    }
}

// MARK: - Stat tile (recessed)

/// A supporting figure inside a card — the recessed tier (web `.hb-sg`):
/// no border, a faint fill, the kicker over one number in the number face.
/// A nil value reads "—".
struct DSRStatTile: View {
    let label: String
    let value: String
    var tone: Color = .cavnarInk
    var detail: String? = nil
    /// A value that is words with figures in it ("Light rain · high 71°"):
    /// drawn in the body face with its figures in the number face, over
    /// two lines, instead of one line of the number face.
    var valueIsText: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs + 1) {
            CavnarKicker(label, isHeader: false)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
            if valueIsText {
                HomeMixedText.make(value, role: .label, color: tone)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(value)
                    .cavnarText(.figureS, color: value == DSRFormat.dash ? Color.cavnarInk3 : tone)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }
            if let detail {
                HomeMixedText.make(detail, role: .caption, color: .cavnarInk2)
                    .lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 11)
        .padding(.vertical, 10)
        .background(Color.cavnarPaper3.opacity(0.35), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityElement(children: .combine)
    }
}

/// Up to three tiles in a row, wrapping to the next row beyond that.
struct DSRTileRow: View {
    let tiles: [DSRStatTile]

    var body: some View {
        let rows = stride(from: 0, to: tiles.count, by: 3).map { Array(tiles[$0..<min($0 + 3, tiles.count)]) }
        VStack(spacing: CavnarSpace.xs) {
            ForEach(rows.indices, id: \.self) { r in
                HStack(alignment: .top, spacing: CavnarSpace.xs) {
                    ForEach(rows[r].indices, id: \.self) { i in rows[r][i] }
                }
            }
        }
    }
}

// MARK: - Kicker

/// The report's old kicker, a thin wrapper on `CavnarKicker`. The report
/// screens use `CavnarKicker` directly (10/8/26); `DSRCategoryMapSheet`
/// (DailyReportSettings.swift, "POS department") still draws this one.
/// Candidate for future cleanup after additional verification.
struct DSRKicker: View {
    let text: String
    var tone: Color = .cavnarEmber2

    var body: some View {
        CavnarKicker(text, tint: tone)
    }
}

// MARK: - Section title

/// A section's title in the report's column: the Clash headline, with an
/// optional ember kicker above it (never one that repeats the screen title).
struct DSRSectionTitle: View {
    var kicker: String? = nil
    let title: String

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            if let kicker { CavnarKicker(kicker) }
            Text(title)
                .cavnarText(.headline)
                .accessibilityAddTraits(.isHeader)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, CavnarSpace.xs)
    }
}

// MARK: - Expandable block card

/// One block of the night — Sales, Labor, Food… The header shows the title
/// with its source under it, a status only when the block is NOT ready,
/// and ONE headline line, so the report reads collapsed in under two
/// minutes; the detail opens on tap. A block that isn't ready shows the
/// server's reason instead of a body, and doesn't expand.
struct DSRBlockCard<Content: View>: View {
    let title: String
    let block: DSRBlock
    var headline: String? = nil
    @Binding var isExpanded: Bool
    @ViewBuilder var content: () -> Content
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                guard block.isReady else { return }
                Haptic.selection()
                if reduceMotion { isExpanded.toggle() } else {
                    withAnimation(.easeOut(duration: 0.3)) { isExpanded.toggle() }
                }
            } label: {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(title).cavnarText(.headline)
                            if let source = block.sourceLabel {
                                Text("From \(source)").cavnarText(.caption)
                                    .lineLimit(1)
                            }
                        }
                        Spacer(minLength: CavnarSpace.xs)
                        if !block.isReady {
                            DSRBlockStatus(status: block.status)
                        } else {
                            Image(systemName: "chevron.down")
                                .font(.cavnar(.secondary))
                                .foregroundStyle(Color.cavnarInk2)
                                .rotationEffect(.degrees(isExpanded ? 180 : 0))
                                .accessibilityHidden(true)
                        }
                    }
                    if block.isReady, let headline {
                        CavnarMixedText(headline, role: .body)
                    } else if !block.isReady {
                        Text(block.reason ?? "Not ready yet.")
                            .cavnarText(.secondary, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(block.isReady ? .isButton : [])
            .accessibilityHint(block.isReady ? (isExpanded ? "Collapses the detail" : "Shows the detail") : "")

            if isExpanded && block.isReady {
                content()
                    .padding(.top, CavnarSpace.m)
                    .transition(reduceMotion ? .identity : .opacity.combined(with: .scale(scale: 0.98, anchor: .top)))
            }
        }
        .cavnarCard()
    }
}

/// "Waiting" / "Unavailable" / "Not connected" beside a block that isn't
/// ready. A ready block shows no badge (10/8/26).
struct DSRBlockStatus: View {
    let status: String?

    var body: some View {
        let (text, tone) = Self.describe(status)
        Text(text)
            .cavnarText(.tag, color: tone.foreground)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(tone.background)
            .clipShape(Capsule())
    }

    static func describe(_ status: String?) -> (String, CavnarTone) {
        switch status {
        // dsr.STATUSES: ready, awaiting, unavailable, not_connected.
        case "ready": return ("Ready", .good)
        case "awaiting": return ("Waiting", .warning)
        case "unavailable": return ("Unavailable", .warning)
        case "not_connected": return ("Not connected", .neutral)
        case nil: return ("Not started", .neutral)
        // A status this build doesn't know is "Not ready" — never the raw
        // server key capitalised ("Item Missing", re-audit D24).
        case .some: return ("Not ready", .warning)
        }
    }
}

// MARK: - Lines

/// A dotted list of the model's sentences — went well (green) or needs
/// attention (amber). Numbers inside the prose are in the number face.
struct DSRLineList: View {
    let lines: [DSRLine]
    let dot: Color

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            ForEach(lines) { line in
                HStack(alignment: .top, spacing: CavnarSpace.s) {
                    Circle().fill(dot).frame(width: 7, height: 7).padding(.top, 8)
                    CavnarMixedText(line.text, role: .body)
                    Spacer(minLength: 0)
                }
            }
        }
    }
}

// MARK: - Charts

/// Sales by hour: ember bars that grow in (motion 10 · bar grow-in), the
/// peak hour lit. An hour the POS reported with no figure is a gap, never
/// a zero-height bar pretending to be one.
struct DSRHourlyBars: View {
    let hours: [DSRBlock.Hour]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var grown = false

    var body: some View {
        let peak = hours.compactMap(\.net).max() ?? 0
        VStack(spacing: 6) {
            HStack(alignment: .bottom, spacing: 4) {
                ForEach(Array(hours.enumerated()), id: \.offset) { index, hour in
                    let isPeak = hour.net != nil && hour.net == peak && peak > 0
                    ZStack(alignment: .bottom) {
                        if let net = hour.net, peak > 0 {
                            RoundedRectangle(cornerRadius: 3, style: .continuous)
                                .fill(LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber.opacity(isPeak ? 1 : 0.75)],
                                                     startPoint: .top, endPoint: .bottom))
                                .frame(height: max(3, 96 * CGFloat(net / peak)))
                                .shadow(color: isPeak ? Color.cavnarEmber.opacity(0.55) : .clear, radius: 8)
                                .scaleEffect(x: 1, y: grown || reduceMotion ? 1 : 0.02, anchor: .bottom)
                                .animation(.easeOut(duration: 0.5).delay(Double(index) * 0.035), value: grown)
                        } else {
                            Rectangle().fill(Color.cavnarInk3.opacity(0.35)).frame(height: 1)
                        }
                    }
                    .frame(maxWidth: .infinity, minHeight: 96, maxHeight: 96, alignment: .bottom)
                    .accessibilityElement()
                    .accessibilityLabel("\(hour.label): \(DSRFormat.money(hour.net))")
                }
            }
            HStack(spacing: 4) {
                ForEach(Array(hours.enumerated()), id: \.offset) { index, hour in
                    // A chart axis: 11pt, the one size allowed under the
                    // caption floor besides a tag (10/8/26).
                    Text(index % 2 == 0 ? hour.label : " ")
                        .font(.cavnarNumber(CavnarType.tag))
                        .foregroundStyle(Color.cavnarInk2)
                        .frame(maxWidth: .infinity)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
                }
            }
        }
        .onAppear { grown = true }
    }
}

/// Categories as horizontal bars against the biggest one.
struct DSRCategoryBars: View {
    let categories: [DSRBlock.Category]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var grown = false

    var body: some View {
        let top = categories.compactMap(\.net).max() ?? 0
        VStack(spacing: 9) {
            ForEach(Array(categories.enumerated()), id: \.offset) { index, cat in
                HStack(spacing: CavnarSpace.s) {
                    Text(cat.name)
                        .cavnarText(.secondary)
                        .frame(width: 92, alignment: .leading)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                            if let net = cat.net, top > 0 {
                                Capsule()
                                    .fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2],
                                                         startPoint: .leading, endPoint: .trailing))
                                    .frame(width: max(4, geo.size.width * CGFloat(net / top)))
                                    .scaleEffect(x: grown || reduceMotion ? 1 : 0.02, y: 1, anchor: .leading)
                                    .animation(.easeOut(duration: 0.5).delay(Double(index) * 0.05), value: grown)
                            }
                        }
                    }
                    .frame(height: 6)
                    Text(DSRFormat.money(cat.net))
                        .font(.cavnarNumber(CavnarType.secondary, weight: 600))
                        .foregroundStyle(cat.net == nil ? Color.cavnarInk3 : Color.cavnarInk)
                        .fixedSize().frame(minWidth: 70, alignment: .trailing).layoutPriority(1)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .onAppear { grown = true }
    }
}

// MARK: - Progress

/// The night being built, stage by stage (access.checklist): a green tick
/// for what's done with the local time it happened, the breathing ember on
/// the stage running now, a hollow ring for what's still to come — then
/// each block's own state.
struct DSRProgressChecklist: View {
    let checklist: DSRChecklist?
    var starting: Bool = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if starting && checklist == nil {
                stageRow(label: "Starting the night", done: false, current: true, time: nil, isLast: true)
            }
            if let checklist {
                // A finished night lists only the stages it passed through
                // (a night the POS closed on its own never waited on it).
                let stages = checklist.phase.isTerminal
                    ? checklist.stages.filter { $0.at != nil || $0.current }
                    : checklist.stages
                ForEach(Array(stages.enumerated()), id: \.element.id) { index, stage in
                    stageRow(label: stage.label, done: stage.done && !stage.current || (stage.current && checklist.phase.isTerminal),
                             current: stage.current && !checklist.phase.isTerminal,
                             time: DSRFormat.localTime(stage.atLocal), isLast: index == stages.count - 1)
                }
                if !checklist.blocks.isEmpty {
                    CavnarKicker("What's in")
                        .padding(.top, 14)
                        .padding(.bottom, 6)
                    ForEach(checklist.blocks) { b in
                        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                            Image(systemName: b.status == "ready" ? "checkmark" : (b.status == nil ? "circle" : "clock"))
                                .font(.cavnar(.caption))
                                .foregroundStyle(b.status == "ready" ? Color.cavnarGreen : Color.cavnarInk3)
                                .frame(width: 14)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(b.label).cavnarText(.label)
                                if b.status != "ready", let reason = b.reason {
                                    Text(reason).cavnarText(.caption, color: .cavnarAmber)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            Spacer(minLength: CavnarSpace.xs)
                            if let t = DSRFormat.localTime(b.atLocal), b.status == "ready" {
                                Text(t).font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                            }
                        }
                        .padding(.vertical, 5)
                        .accessibilityElement(children: .combine)
                    }
                }
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.3), value: checklist?.stages.map(\.done))
    }

    private func stageRow(label: String, done: Bool, current: Bool, time: String?, isLast: Bool) -> some View {
        HStack(alignment: .top, spacing: CavnarSpace.s) {
            VStack(spacing: 0) {
                ZStack {
                    if current {
                        BreathingDot(color: .cavnarEmber, paused: reduceMotion)
                    } else if done {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(Color.cavnarGreen)
                    } else {
                        Circle().strokeBorder(Color.cavnarInk3, lineWidth: 1.5).frame(width: 13, height: 13)
                    }
                }
                .frame(width: 18, height: 18)
                if !isLast {
                    Rectangle().fill(done ? Color.cavnarGreen.opacity(0.45) : Color.cavnarPaper3)
                        .frame(width: 1.5, height: 16)
                }
            }
            Text(label)
                .font(.cavnarBody(CavnarType.body, weight: current ? 700 : 400))
                .foregroundStyle(done || current ? Color.cavnarInk : Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: CavnarSpace.xs)
            if let time {
                Text(time).font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2).padding(.top, 2)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label), \(current ? "running now" : (done ? "done" : "to come"))\(time.map { ", \($0)" } ?? "")")
    }
}
