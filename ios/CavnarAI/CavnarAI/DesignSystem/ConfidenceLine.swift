import SwiftUI

// MARK: - Tone colours

extension ConfidenceDisplay.Tone {
    /// ≥75 green, 50–74 ink2, below 50 or not measurable amber. Never red,
    /// never ember: a confidence is not a verdict, and ember is not status
    /// (DESIGN_SYSTEM §9).
    var color: Color {
        switch self {
        case .good: return .cavnarGreen
        case .neutral: return .cavnarInk2
        case .warn: return .cavnarAmber
        }
    }
}

extension ServerTone {
    /// The status colour for a tone the server decided (I10: one table for
    /// label and colour): good green, warn amber, bad red, neutral ink2.
    /// Nil when the server sent none — the caller's own fallback applies.
    /// Never ember: ember is not status (DESIGN_SYSTEM §9).
    var color: Color? {
        switch value {
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        case "bad": return .cavnarRed
        case "neutral": return .cavnarInk2
        default: return nil
        }
    }
}

// MARK: - The meter

/// A small percentage meter: a Paper3 capsule track with a gradient fill in
/// the tone colour and a soft glow, grown in from zero (still under Reduce
/// Motion). A nil fraction draws the empty track — "not measured" is never
/// drawn as a zero-width fill that could read as 0%.
struct ConfidenceMeter: View {
    let fraction: Double?
    let tone: ConfidenceDisplay.Tone
    var width: CGFloat? = 34
    var height: CGFloat = 5

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var grown = false

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.cavnarPaper3.opacity(0.9))
                if let fraction {
                    let f = CGFloat(max(0, min(1, fraction)))
                    Capsule()
                        .fill(LinearGradient(colors: [tone.color.opacity(0.55), tone.color],
                                             startPoint: .leading, endPoint: .trailing))
                        .frame(width: max(height, geo.size.width * (grown ? f : 0)))
                        .shadow(color: tone.color.opacity(0.55), radius: height * 0.9, x: 0, y: 0)
                        .opacity(grown ? 1 : 0)
                }
            }
        }
        .frame(width: width, height: height)
        .onAppear {
            if reduceMotion {
                grown = true
            } else {
                withAnimation(.easeOut(duration: 0.7).delay(0.1)) { grown = true }
            }
        }
        .accessibilityHidden(true)
    }
}

// MARK: - The line

/// "▬ 72% confidence — only 3 reviews in 90 days  Why?" — ONE confidence
/// line for every recommendation on the phone (FIXLIST J1): Home cards,
/// Needs attention, the one-thing hero, the review / food / labor
/// diagnoses, food drivers, Ask answers and the daily report's actions.
///
/// "Why?" opens `ConfidenceWhySheet` — the three things the figure rests on
/// — and records that the owner looked at the evidence when the line
/// belongs to a keyed recommendation. It only appears when the server sent
/// the dimensions (a legacy band has nothing more to show).
struct ConfidenceLine: View {
    let confidence: TrustConfidence
    var recKey: String? = nil
    var surface: String
    var module: String
    /// The deck's fixed-height card: no reason or caution inline — both are
    /// in the sheet.
    var compact: Bool = false

    @State private var showingWhy = false

    private var display: ConfidenceDisplay { ConfidenceDisplay(confidence) }

    var body: some View {
        let d = display
        if d.isRenderable {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if d.meterFraction != nil || confidence.isMeasuredShape {
                        ConfidenceMeter(fraction: d.meterFraction, tone: d.tone)
                            .alignmentGuide(.firstTextBaseline) { dim in dim[.bottom] + 1 }
                    }
                    lineText(d)
                        .lineLimit(compact ? 1 : nil)
                        .fixedSize(horizontal: false, vertical: !compact)
                        .accessibilityLabel(d.accessibilityLabel)
                    if d.showsWhy {
                        Button {
                            Haptic.light()
                            showingWhy = true
                            RecEvidenceLog.viewed(key: recKey, surface: surface, module: module)
                        } label: {
                            Text("Why?")
                                .font(.cavnarBody(12.5, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                                .padding(.vertical, 4)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint("Shows what this confidence rests on")
                    }
                    Spacer(minLength: 0)
                }
                if !compact, let caution = d.caution {
                    HomeMixedText.make(caution, size: 12.5, weight: 500, color: .cavnarAmber)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .sheet(isPresented: $showingWhy) {
                ConfidenceWhySheet(confidence: confidence)
            }
        }
    }

    private func lineText(_ d: ConfidenceDisplay) -> Text {
        let label = HomeMixedText.make(d.lineLabel, size: 12.5, weight: 700, color: d.tone.color)
        guard !compact, let reason = d.reason, !reason.isEmpty else { return label }
        return label + HomeMixedText.make(" \u{2014} " + reason, size: 12.5, weight: 500, color: .cavnarInk3)
    }
}

// MARK: - The sheet

/// What the confidence rests on: the overall figure, then Evidence strength,
/// Historical accuracy and Data freshness — each its percentage (or "—" and
/// what it still needs), a meter, its basis and its detail line. Built from
/// the Account sheet kit.
struct ConfidenceWhySheet: View {
    let confidence: TrustConfidence

    private var display: ConfidenceDisplay { ConfidenceDisplay(confidence) }

    var body: some View {
        let d = display
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    hero(d)
                    AccountSection(kicker: "What it rests on") {
                        ForEach(Array(d.rows.enumerated()), id: \.offset) { i, row in
                            ConfidenceDimensionRow(row: row, showsDivider: i < d.rows.count - 1)
                        }
                    }
                    if let caution = d.caution {
                        CavnarCaveat(title: "Worth knowing", detail: caution)
                    }
                    HomeMixedText.make(d.footer, size: 12.5, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(20)
            }
            .accountSheetChrome("Confidence")
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private func hero(_ d: ConfidenceDisplay) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            AccountKicker(text: "How sure is this")
            // What the figure means, first (the owner's support-score
            // decision, 9/24/26): how well supported — not the chance it works.
            if let meaning = d.meaning {
                Text(meaning + ".")
                    .font(.cavnarBody(13.5, weight: 500))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let pct = d.pct {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("\(pct)%")
                        .font(.cavnarNumber(40, weight: 600))
                        .foregroundStyle(d.tone.color)
                        .cavnarNumberGlow(d.tone.color)
                    Text("confidence")
                        .font(.cavnarBody(15.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(pct) percent confidence")
            } else {
                HomeMixedText.make(d.lineLabel, size: 22, weight: 600, color: d.tone.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ConfidenceMeter(fraction: d.meterFraction, tone: d.tone, width: nil, height: 8)
                .frame(maxWidth: .infinity)
            if let reason = d.reason {
                HomeMixedText.make(reason, size: 15, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

}

/// One "What it rests on" row of a Why? sheet — the title and its %, a wide
/// meter, the basis and detail lines. The confidence sheet's rows, and the
/// comparison-strength sheet's (HowYouCompareCard), draw through this one
/// view so the two panels read as one component.
struct ConfidenceDimensionRow: View {
    let row: ConfidenceDisplay.Row
    var showsDivider: Bool = true

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            AccountKVRow(label: row.title, showsDivider: false) {
                Text(row.value)
                    .font(.cavnarNumber(18, weight: 600))
                    .foregroundStyle(row.tone.color)
            }
            VStack(alignment: .leading, spacing: 5) {
                ConfidenceMeter(fraction: row.meterFraction, tone: row.tone, width: nil, height: 6)
                    .frame(maxWidth: .infinity)
                if !row.basis.isEmpty {
                    HomeMixedText.make(row.basis, size: 13.5, weight: 500, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let detail = row.detail {
                    HomeMixedText.make(detail, size: 12.5, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let note = row.note {
                    HomeMixedText.make(note, size: 12.5, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.bottom, 12)
            if showsDivider { AccountRowDivider() }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(row.title): \(row.value == "\u{2014}" ? "not measured" : row.value). \(row.basis)"
                            + (row.detail.map { ". \($0)" } ?? "") + (row.note.map { ". \($0)" } ?? ""))
    }
}

// MARK: - Claim kind

/// "MEASURED" / "COMPUTED" / "FORECAST" / "INFERRED" / "AI-WRITTEN" — a
/// tiny uppercase tag in ink3 on a Paper3 capsule. Quiet on purpose: it
/// says what kind of statement a line is, not how good the news is, so it
/// never takes a status colour or the ember. Renders nothing for a kind it
/// does not know.
struct ClaimKindTag: View {
    let kind: String?
    var modelWritten: Bool? = nil

    var body: some View {
        if let label = ClaimKind.label(kind: kind, modelWritten: modelWritten) {
            Text(label.uppercased())
                .font(.cavnarBody(10.5, weight: 700))
                .tracking(1)
                .foregroundStyle(Color.cavnarInk3)
                .padding(.horizontal, 7)
                .padding(.vertical, 2)
                .background(Color.cavnarPaper3, in: Capsule())
                .fixedSize()
                .accessibilityLabel(label)
        }
    }
}
