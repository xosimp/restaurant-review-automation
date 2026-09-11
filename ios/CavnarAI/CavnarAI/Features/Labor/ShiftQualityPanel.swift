import SwiftUI

/// Shift Quality — the schedule graded the way an operator reads one.
///
/// The generator used to answer three small questions well (who is free,
/// what does it cost, how many bodies) and nothing judged the result as a
/// whole. This is that judgement, and the whole point is that a manager
/// can act on it in under thirty seconds: one number, the handful of
/// dimensions behind it, how much the engine actually knew, and then the
/// detail only if they want it.
///
/// Everything on screen is computed deterministically from the finished
/// schedule. No sentence here came from a model, which is why none of them
/// can name a person or a shift that is not in the week.
struct ShiftQualityPanel: View {
    let quality: ScheduleQuality
    var whatIf: ScheduleWhatIf? = nil
    /// Set while a manager's edit is being re-scored, so the number reads
    /// as catching up rather than as the new truth.
    var isRescoring: Bool = false

    @State private var expandedShift: String?
    @State private var showingReasoning = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            if let dimensions = customerDimensions, !dimensions.isEmpty {
                dimensionGrid(dimensions)
            }
            if !warnings.isEmpty { warningBlock }
            shiftStrip
            reasoningBlock
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Color.cavnarPaper2.opacity(0.6))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(tone.opacity(0.35), lineWidth: 1)
        )
        .animation(.easeOut(duration: 0.22), value: expandedShift)
        .animation(.easeOut(duration: 0.22), value: showingReasoning)
    }

    // MARK: The number

    private var header: some View {
        HStack(alignment: .center, spacing: 16) {
            QualityDial(score: quality.score ?? 0, tone: tone, muted: isRescoring)
            VStack(alignment: .leading, spacing: 5) {
                Text("SHIFT QUALITY")
                    .font(.cavnarBody(11.5, weight: 700))
                    .tracking(1.4)
                    .foregroundStyle(Color.cavnarInk3)
                Text(bandLabel)
                    .font(.cavnarHeadline(19))
                    .foregroundStyle(Color.cavnarInk)
                if let confidence = quality.confidence {
                    confidencePill(confidence)
                }
            }
            Spacer(minLength: 0)
        }
    }

    private func confidencePill(_ confidence: QualityConfidence) -> some View {
        HStack(spacing: 5) {
            Circle()
                .fill(confidenceTone(confidence.level))
                .frame(width: 6, height: 6)
            Text("\(confidence.label) confidence")
                .font(.cavnarBody(12.5, weight: 600))
                .foregroundStyle(Color.cavnarInk2)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Capsule().fill(Color.cavnarPaper3.opacity(0.7)))
    }

    // MARK: Dimensions

    private var customerDimensions: [QualityDimension]? {
        let dims = quality.customerDimensions
        return dims.isEmpty ? nil : dims
    }

    private func dimensionGrid(_ dimensions: [QualityDimension]) -> some View {
        VStack(spacing: 9) {
            ForEach(dimensions) { dimension in
                HStack(spacing: 10) {
                    Text(dimension.label)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk2)
                        .frame(width: 122, alignment: .leading)
                    QualityBar(score: dimension.score, tone: toneFor(dimension.score))
                    Text("\(dimension.score)%")
                        .font(.cavnarNumber(13, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                        .frame(width: 40, alignment: .trailing)
                }
            }
        }
    }

    // MARK: Warnings

    /// Stated calmly and with the action attached. A shift under its bar is
    /// information the manager can fix in thirty seconds, not an alarm.
    private var warnings: [String] {
        var out = (quality.belowProfile ?? []).map { shift in
            "\(shift.day) \(shift.daypart == "morning" ? "lunch" : "dinner") came in at "
            + "\(shift.score), under the \(shift.minQuality) this \(shift.label.lowercased()) expects."
        }
        out.append(contentsOf: quality.recommendations ?? [])
        return Array(out.prefix(5))
    }

    private var warningBlock: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(warnings, id: \.self) { line in
                HStack(alignment: .top, spacing: 7) {
                    Circle()
                        .fill(Color.cavnarAmber)
                        .frame(width: 5, height: 5)
                        .padding(.top, 6)
                    Text(line)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.cavnarAmber.opacity(0.07))
        )
    }

    // MARK: Per-shift

    private var shiftStrip: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("EVERY SHIFT")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.3)
                .foregroundStyle(Color.cavnarInk3)
            ForEach(quality.scoredShifts) { shift in
                shiftRow(shift)
            }
        }
    }

    private func shiftRow(_ shift: QualityShift) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                Haptic.selection()
                expandedShift = expandedShift == shift.id ? nil : shift.id
            } label: {
                HStack(spacing: 10) {
                    Text(shift.title)
                        .font(.cavnarBody(14, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    if shift.profile.demand == "peak" || shift.profile.demand == "high" {
                        Text(shift.profile.demand == "peak" ? "PEAK" : "BUSY")
                            .font(.cavnarBody(9.5, weight: 700))
                            .tracking(0.6)
                            .foregroundStyle(Color.cavnarEmber)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(Color.cavnarEmber.opacity(0.14)))
                    }
                    if shift.profile.trainingAllowed == true {
                        Text("TRAINING")
                            .font(.cavnarBody(9.5, weight: 700))
                            .tracking(0.6)
                            .foregroundStyle(Color.cavnarBlue)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(Color.cavnarBlue.opacity(0.14)))
                    }
                    Spacer(minLength: 6)
                    Text("\(shift.score ?? 0)")
                        .font(.cavnarNumber(15, weight: 700))
                        .foregroundStyle(toneFor(shift.score ?? 0))
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.cavnarInk3)
                        .rotationEffect(.degrees(expandedShift == shift.id ? 180 : 0))
                }
                .contentShape(Rectangle())
                .padding(.vertical, 7)
            }
            .buttonStyle(.plain)

            if expandedShift == shift.id {
                shiftDetail(shift)
                    .padding(.bottom, 8)
                    .transition(.opacity)
            }
            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
        }
    }

    /// Why this shift scored what it did — chosen people, what held it back,
    /// and anything the engine could not see. Kept to what a manager would
    /// read standing at the pass.
    private func shiftDetail(_ shift: QualityShift) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let people = shift.people, !people.isEmpty {
                Text("On this shift: " + people.joined(separator: ", "))
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach((shift.strengths ?? []).prefix(3), id: \.self) { line in
                detailLine(line, symbol: "checkmark", color: .cavnarGreen)
            }
            ForEach((shift.weaknesses ?? []).prefix(4), id: \.self) { line in
                detailLine(line, symbol: "exclamationmark", color: .cavnarAmber)
            }
            ForEach((shift.blindSpots ?? []).prefix(2), id: \.self) { line in
                detailLine(line, symbol: "questionmark", color: .cavnarInk3)
            }
            if let capped = shift.cappedBy {
                Text("Capped by \(capped.replacingOccurrences(of: "_", with: " ")) — a shift is never better than its weakest critical part.")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let dimensions = shift.dimensions?.filter({ $0.isCustomerFacing }), !dimensions.isEmpty {
                HStack(spacing: 6) {
                    ForEach(dimensions) { d in
                        VStack(spacing: 2) {
                            Text("\(d.score)")
                                .font(.cavnarNumber(12, weight: 700))
                                .foregroundStyle(toneFor(d.score))
                            Text(shortLabel(d.label))
                                .font(.cavnarBody(9))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                        .background(
                            RoundedRectangle(cornerRadius: 7, style: .continuous)
                                .fill(Color.cavnarPaper3.opacity(0.5)))
                    }
                }
                .padding(.top, 2)
            }
        }
    }

    private func detailLine(_ text: String, symbol: String, color: Color) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: symbol)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(color)
                .frame(width: 12)
                .padding(.top, 3)
            Text(text)
                .font(.cavnarBody(12.5))
                .foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Why this schedule

    private var reasoningBlock: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                Haptic.selection()
                showingReasoning.toggle()
            } label: {
                HStack(spacing: 6) {
                    Text(showingReasoning ? "Hide reasoning" : "Why this schedule?")
                        .font(.cavnarBody(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber)
                        .rotationEffect(.degrees(showingReasoning ? 180 : 0))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if showingReasoning {
                VStack(alignment: .leading, spacing: 9) {
                    if let whatIf, whatIf.ran, let verdict = whatIf.verdict {
                        Text(verdict)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                        ForEach((whatIf.swaps ?? []).prefix(3)) { swap in
                            detailLine(swap.reason, symbol: "arrow.left.arrow.right",
                                       color: .cavnarBlue)
                        }
                    }
                    ForEach((quality.strengths ?? []).prefix(3), id: \.self) { line in
                        detailLine(line, symbol: "checkmark", color: .cavnarGreen)
                    }
                    ForEach((quality.weaknesses ?? []).prefix(3), id: \.self) { line in
                        detailLine(line, symbol: "exclamationmark", color: .cavnarAmber)
                    }
                    if let confidence = quality.confidence, !confidence.reasons.isEmpty {
                        Text(confidence.summary)
                            .font(.cavnarBody(12.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk2)
                            .padding(.top, 2)
                        ForEach(confidence.reasons, id: \.self) { reason in
                            detailLine(reason, symbol: "questionmark", color: .cavnarInk3)
                        }
                    }
                }
                .transition(.opacity)
            }
        }
    }

    // MARK: Tone

    private var tone: Color { toneFor(quality.score ?? 0) }

    private func toneFor(_ score: Int) -> Color {
        switch score {
        case 90...: return .cavnarGreen
        case 78..<90: return .cavnarGreen.opacity(0.8)
        case 65..<78: return .cavnarAmber
        default: return .cavnarRed
        }
    }

    private func confidenceTone(_ level: String) -> Color {
        switch level {
        case "high": return .cavnarGreen
        case "moderate": return .cavnarAmber
        default: return .cavnarRed
        }
    }

    private var bandLabel: String {
        guard let band = quality.band else { return "Not scored" }
        return band.prefix(1).uppercased() + band.dropFirst()
    }

    /// The dimension chips inside an expanded shift sit four or five across,
    /// so the full labels have to give.
    private func shortLabel(_ label: String) -> String {
        ["Operational strength": "Strength", "Labor efficiency": "Labor",
         "Training balance": "Training", "Demand match": "Demand"][label] ?? label
    }
}

/// The score itself, as an arc that fills to it.
///
/// A ring rather than a number alone because the whole promise of this
/// screen is that a manager understands the week at a glance, and a
/// three-quarter-full green arc reads before any digit does.
private struct QualityDial: View {
    let score: Int
    let tone: Color
    var muted: Bool = false

    var body: some View {
        ZStack {
            Circle()
                .stroke(Color.cavnarPaper3, lineWidth: 7)
            Circle()
                .trim(from: 0, to: CGFloat(max(0, min(100, score))) / 100)
                .stroke(
                    AngularGradient(
                        colors: [tone.opacity(0.55), tone, tone.opacity(0.9)],
                        center: .center, startAngle: .degrees(0), endAngle: .degrees(360)),
                    style: StrokeStyle(lineWidth: 7, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .shadow(color: tone.opacity(0.5), radius: 6)
            VStack(spacing: -2) {
                Text("\(score)")
                    .font(.cavnarNumber(26, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text("/100")
                    .font(.cavnarNumber(10))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(width: 84, height: 84)
        .opacity(muted ? 0.45 : 1)
        .animation(.easeOut(duration: 0.45), value: score)
    }
}

/// One dimension's bar. Gradient rather than a flat fill so a 91 and a 62
/// are different at a glance and not only by length.
private struct QualityBar: View {
    let score: Int
    let tone: Color

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.cavnarPaper3.opacity(0.8))
                Capsule()
                    .fill(LinearGradient(colors: [tone.opacity(0.55), tone],
                                         startPoint: .leading, endPoint: .trailing))
                    .frame(width: max(4, geo.size.width * CGFloat(max(0, min(100, score))) / 100))
            }
        }
        .frame(height: 7)
        .animation(.easeOut(duration: 0.4), value: score)
    }
}
