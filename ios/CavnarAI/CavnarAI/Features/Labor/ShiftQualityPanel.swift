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
    var overrideState: LaborViewModel.OverrideState = .idle
    /// LaborViewModel.savedTick — a change shows "Change saved" for a few
    /// seconds each time this moves.
    var savedTick: Int = 0
    @State private var showingSaved = false
    /// What the owner has said about each recommendation ("accepted" /
    /// "dismissed", by text) and where a ✓ or ✕ goes. When no handler is
    /// given the recommendations read as plain warnings, as before.
    var recommendationDecisions: [String: String] = [:]
    /// (text, accepted, reason). ✕ asks why first (RecReasonDialog): a
    /// reason arrives with it, Skip arrives as nil, Cancel never calls.
    var onRecommendation: ((String, Bool, RecReason?) -> Void)? = nil
    var suppressedKinds: [String] = []
    /// The generation loop's view model, when the panel sits on the live
    /// draft: the score's movement, what Cavnar changed, rating in place
    /// and the per-shift what-if all read from it. Nil renders the verdict
    /// alone.
    var viewModel: LaborViewModel? = nil

    /// The recommendation whose ✕ is asking why — held apart from the
    /// dialog's own flag (DESIGN_SYSTEM → Why not).
    @State private var reasonFor: String?
    @State private var askingReason = false
    @State private var expandedShift: String?
    @State private var showingReasoning = false
    @State private var showingChanges = false
    // Per-shift what-if: who, in place of which row (nil = added), and the
    // answer the score endpoint gave, by shift id.
    @State private var whatIfWho: [String: String] = [:]
    @State private var whatIfFor: [String: String] = [:]
    @State private var whatIfAnswer: [String: WhatIfAnswer] = [:]
    @State private var whatIfBusy: String?

    /// What one what-if came back with. "Put them on" rebuilds the change
    /// from the week as it is at that moment (`replacing` names the row),
    /// never from rows captured when it was scored — edits made since would
    /// be thrown away. A failed what-if is not `applicable`.
    struct WhatIfAnswer: Equatable {
        let text: String
        let who: String
        let date: String
        let replacing: String?
        let applicable: Bool
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            header
            overrideLine
                .task(id: savedTick) {
                    // Shown for a moment after each save, then gone — held
                    // here rather than by the view model (CLIENT-62).
                    guard savedTick > 0 else { return }
                    showingSaved = true
                    try? await Task.sleep(for: .seconds(4))
                    showingSaved = false
                }
            if let optimizer = viewModel?.scheduleResult?.optimizerSummary, optimizer.hasContent {
                optimizerBlock(optimizer)
            }
            if let reason = viewModel?.scheduleResult?.gate?.reason, viewModel?.scheduleResult?.gate?.ran == true {
                HomeMixedText.make(reason, size: 13, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let viewModel, !viewModel.unratedByHours.isEmpty {
                ratePrompt(viewModel)
            }
            if let dimensions = customerDimensions, !dimensions.isEmpty {
                dimensionGrid(dimensions)
            }
            if let week = quality.weekDimensions, !week.isEmpty {
                weekMeasures(week)
            }
            if !warnings.isEmpty { warningBlock }
            if onRecommendation != nil, let recs = quality.recommendations, !recs.isEmpty { recommendationsBlock(recs) }
            if !suppressedKinds.isEmpty { hiddenKindsNote }
            shiftStrip
            reasoningBlock
        }
        .padding(18)
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
        .animation(.easeOut(duration: 0.22), value: showingChanges)
        .task(id: quality.needsRatings) {
            // The rating prompt needs to know who is rated already.
            if quality.needsRatings, let viewModel, viewModel.team.isEmpty { await viewModel.loadTeam() }
        }
    }

    // MARK: The number

    private var header: some View {
        HStack(alignment: .center, spacing: 16) {
            QualityDial(score: quality.score ?? 0, tone: tone, muted: isRescoring)
            VStack(alignment: .leading, spacing: 5) {
                Text("SHIFT QUALITY")
                    .font(.cavnarBody(12.5, weight: 700))
                    .tracking(1.4)
                    .foregroundStyle(Color.cavnarInk3)
                HStack(spacing: 8) {
                    Text(bandLabel)
                        .font(.cavnarHeadline(23))
                        .foregroundStyle(Color.cavnarInk)
                    if quality.isProvisional {
                        Text("PROVISIONAL")
                            .font(.cavnarBody(12, weight: 700))
                            .tracking(0.8)
                            .foregroundStyle(Color.cavnarAmber)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(Color.cavnarAmber.opacity(0.14)))
                    }
                    if let delta = viewModel?.scoreDelta {
                        ScoreDeltaChip(delta: delta)
                    }
                }
                if let confidence = quality.confidence {
                    confidencePill(confidence)
                    // Low confidence says why first — the top reason.
                    if quality.isProvisional, let top = confidence.reasons.first {
                        HomeMixedText.make(top, size: 13, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
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
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk2)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .background(Capsule().fill(Color.cavnarPaper3.opacity(0.7)))
    }

    /// What happened to the manager's last edit. A silent failure left the
    /// old number on screen beside a CHANGED badge implying it was current.
    @ViewBuilder
    private var overrideLine: some View {
        switch overrideState {
        case .idle:
            if showingSaved {
                Text("Change saved — this is what your staff will receive.")
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
            }
        case .saving:
            Text("Saving your change…")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
        case .failed(let message):
            Text("\(message) The score above is out of date.")
                .font(.cavnarBody(13.5, weight: 600))
                .foregroundStyle(Color.cavnarRed)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Dimensions

    private var customerDimensions: [QualityDimension]? {
        let dims = quality.customerDimensions
        return dims.isEmpty ? nil : dims
    }

    private func dimensionGrid(_ dimensions: [QualityDimension]) -> some View {
        VStack(spacing: 11) {
            ForEach(dimensions) { dimension in
                HStack(spacing: 10) {
                    Text(dimension.label)
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk2)
                        .frame(width: 132, alignment: .leading)
                    QualityBar(score: dimension.score, tone: toneFor(dimension.score))
                    Text("\(dimension.score)%")
                        .font(.cavnarNumber(14.5, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                        .frame(width: 46, alignment: .trailing)
                }
            }
        }
    }

    /// The measures of the whole week (fatigue), judged once per person and
    /// weighted once — the same rows as the grid, under their own kicker,
    /// with the finding and the part of the week score each carries.
    private func weekMeasures(_ dims: [QualityWeekDimension]) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            AccountKicker(text: "Across the week")
            ForEach(dims) { dim in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 10) {
                        Text(dim.label)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk2)
                            .frame(width: 132, alignment: .leading)
                        QualityBar(score: dim.score, tone: toneFor(dim.score))
                        Text("\(dim.score)%")
                            .font(.cavnarNumber(14.5, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                            .frame(width: 46, alignment: .trailing)
                    }
                    if let why = dim.why {
                        HomeMixedText.make(
                            why + (dim.shareText.map { " \($0) of the week score." } ?? ""),
                            size: 13, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    // MARK: What Cavnar changed

    /// "Cavnar improved this draft from 71 to 84 — 5 changes", the reason
    /// for each change behind a disclosure, and what no legal change could
    /// fix — the owner's to decide.
    private func optimizerBlock(_ o: ScheduleOptimizer) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            if let headline = o.headline {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    HomeMixedText.make(headline, size: 14.5, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if viewModel?.optimizerUnsaved == true {
                        Text("NOT SAVED")
                            .font(.cavnarBody(12, weight: 700))
                            .tracking(0.8)
                            .foregroundStyle(Color.cavnarEmber)
                    }
                }
                Button {
                    Haptic.selection()
                    showingChanges.toggle()
                } label: {
                    HStack(spacing: 6) {
                        Text(showingChanges ? "Hide the changes" : "What changed and why")
                            .font(.cavnarBody(14, weight: 700))
                            .foregroundStyle(Color.cavnarEmber)
                        Image(systemName: "chevron.down")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                            .rotationEffect(.degrees(showingChanges ? 180 : 0))
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if showingChanges {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(o.changes ?? []) { change in
                            HStack(alignment: .top, spacing: 8) {
                                Image(systemName: "checkmark")
                                    .font(.system(size: 10, weight: .bold))
                                    .foregroundStyle(Color.cavnarGreen)
                                    .frame(width: 13)
                                    .padding(.top, 4)
                                HomeMixedText.make(change.reason, size: 14, color: .cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                                Spacer(minLength: 4)
                                if let gain = change.gain, gain > 0 {
                                    Text("+\(Int(gain.rounded()))")
                                        .font(.cavnarNumber(13, weight: 700))
                                        .foregroundStyle(Color.cavnarGreen)
                                }
                            }
                        }
                    }
                    .transition(.opacity)
                }
            } else if let verdict = o.verdict {
                HomeMixedText.make(verdict, size: 14, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let unresolved = o.unresolved, !unresolved.isEmpty {
                Text("STILL NEEDS YOU")
                    .font(.cavnarBody(12, weight: 700))
                    .tracking(1.1)
                    .foregroundStyle(Color.cavnarRed)
                    .padding(.top, 2)
                ForEach(unresolved) { item in
                    detailLine(item.text, symbol: "circle.fill", color: .cavnarRed)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.ai)
    }

    // MARK: Rating in place

    /// Most of the week unrated: the people carrying the most hours, each
    /// with the same five-number control the Operational Score list uses.
    private func ratePrompt(_ viewModel: LaborViewModel) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Most of this week has no Operational Score, so the number is provisional. Rate the people carrying the most hours:")
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(viewModel.unratedByHours) { person in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Text(person.name)
                            .font(.cavnarBody(14.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make("\(CavnarQualityFormat.hours(person.hours))h this week", size: 13, color: .cavnarInk3)
                    }
                    HStack(spacing: 6) {
                        ForEach(1...5, id: \.self) { value in
                            let on = viewModel.ratedInPrompt[person.name] == value
                            Button {
                                Haptic.light()
                                Task { await viewModel.rateFromPrompt(person.name, score: value) }
                            } label: {
                                Text("\(value)")
                                    .font(.cavnarNumber(14, weight: 700))
                                    .frame(maxWidth: .infinity, minHeight: 30)
                                    .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                                    .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
                                        .fill(on ? Color.cavnarEmber : Color.cavnarPaper2))
                                    .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                                        .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Rate \(person.name) \(value)")
                        }
                    }
                }
            }
            if !viewModel.ratedInPrompt.isEmpty {
                Button {
                    Haptic.medium()
                    Task { await viewModel.rescoreLive() }
                } label: {
                    Group {
                        if viewModel.isRescoringQuality {
                            CavnarShimmerText(text: "Scoring…")
                        } else {
                            Text("Re-score with these ratings")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.isRescoringQuality)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarPaper3.opacity(0.35)))
    }

    // MARK: Warnings

    /// Stated calmly and with the action attached. A shift under its bar is
    /// information the manager can fix in thirty seconds, not an alarm.
    private var warnings: [String] {
        var out = (quality.belowProfile ?? []).map { shift in
            "\(shift.day) \(shift.daypart == "morning" ? "lunch" : "dinner") came in at "
            + "\(shift.score), under the \(shift.minQuality) this \(shift.label.lowercased()) expects."
        }
        // With a decision handler the recommendations get their own block
        // below, each with ✓ / ✕; without one they read here as before.
        if onRecommendation == nil { out.append(contentsOf: quality.recommendations ?? []) }
        return Array(out.prefix(5))
    }

    // MARK: Recommendations

    /// Each one with ✓ (did it) / ✕ (not for us). The answer goes to the
    /// ledger that decides which kinds keep being shown.
    private func recommendationsBlock(_ recs: [String]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("RECOMMENDATIONS")
                .font(.cavnarBody(12, weight: 700))
                .tracking(1.3)
                .foregroundStyle(Color.cavnarInk3)
            ForEach(recs, id: \.self) { rec in
                let decision = recommendationDecisions[rec]
                HStack(alignment: .top, spacing: 8) {
                    Circle()
                        .fill(decision == "accepted" ? Color.cavnarGreen : (decision == "dismissed" ? Color.cavnarInk3 : Color.cavnarAmber))
                        .frame(width: 5, height: 5)
                        .padding(.top, 6)
                    HomeMixedText.make(rec, size: 14.5, color: decision == "dismissed" ? .cavnarInk3 : .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                        .strikethrough(decision == "dismissed", color: Color.cavnarInk3)
                    Spacer(minLength: 4)
                    HStack(spacing: 6) {
                        decisionButton("checkmark", on: decision == "accepted", tone: .cavnarGreen,
                                       label: "Did it") { onRecommendation?(rec, true, nil) }
                        decisionButton("xmark", on: decision == "dismissed", tone: .cavnarInk3,
                                       label: "Not for us") {
                            reasonFor = rec
                            askingReason = true
                        }
                    }
                }
                // An accepted hours change is measured — say on what, until when.
                if decision == "accepted", let tracking = viewModel?.recommendationTracking[rec] {
                    RecTrackerLine(text: tracking)
                        .padding(.leading, 13)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.cavnarAmber.opacity(0.07))
        )
        .recReasonDialog(isPresented: $askingReason, skipLabel: "Skip", onSkip: {
            if let rec = reasonFor { onRecommendation?(rec, false, nil) }
            reasonFor = nil
        }) { reason in
            if let rec = reasonFor { onRecommendation?(rec, false, reason) }
            reasonFor = nil
        }
    }

    private func decisionButton(_ symbol: String, on: Bool, tone: Color, label: String,
                                action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(on ? Color.cavnarPaper : tone)
                .frame(width: 28, height: 28)
                .background(Circle().fill(on ? tone : tone.opacity(0.12)))
                .overlay(Circle().strokeBorder(tone.opacity(on ? 0 : 0.35), lineWidth: 1))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    /// Kinds the engine left out because the owner never acts on them.
    /// The intel card lists them and says how to bring one back.
    private var hiddenKindsNote: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 6) {
                Image(systemName: "eye.slash")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.top, 2)
                HomeMixedText.make(
                    "\(suppressedKinds.count) recommendation \(suppressedKinds.count == 1 ? "kind" : "kinds") hidden — you set them aside. Coverage, leadership and fatigue are never hidden.",
                    size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let vm = viewModel {
                ForEach(suppressedKinds, id: \.self) { kind in
                    Button {
                        Task { await vm.restoreRecommendationKind(kind) }
                    } label: {
                        Text("Show \(kind) again")
                            .font(.cavnarBody(13, weight: 700))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
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
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.cavnarAmber.opacity(0.07))
        )
    }

    // MARK: Per-shift

    private var shiftStrip: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("EVERY SHIFT")
                .font(.cavnarBody(12, weight: 700))
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
                        .font(.cavnarBody(15.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .lineLimit(1)
                        .layoutPriority(1)
                    if shift.profile.demand == "peak" || shift.profile.demand == "high" {
                        Text(shift.profile.demand == "peak" ? "PEAK" : "BUSY")
                            .font(.cavnarBody(11, weight: 700))
                            .tracking(0.6)
                            .foregroundStyle(Color.cavnarEmber)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(Color.cavnarEmber.opacity(0.14)))
                    }
                    if shift.profile.trainingAllowed == true {
                        Text("TRAINING")
                            .font(.cavnarBody(11, weight: 700))
                            .tracking(0.6)
                            .foregroundStyle(Color.cavnarBlue)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(Color.cavnarBlue.opacity(0.14)))
                    }
                    // A bar per shift, so the week's shape reads without
                    // anybody comparing seven near-identical two-digit
                    // numbers to each other.
                    QualityBar(score: shift.score ?? 0, tone: toneFor(shift.score ?? 0))
                        .frame(minWidth: 44)
                    Text("\(shift.score ?? 0)")
                        .font(.cavnarNumber(17, weight: 700))
                        .foregroundStyle(toneFor(shift.score ?? 0))
                        .frame(width: 30, alignment: .trailing)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(expandedShift == shift.id
                                         ? toneFor(shift.score ?? 0) : Color.cavnarInk3)
                        .rotationEffect(.degrees(expandedShift == shift.id ? 180 : 0))
                }
                .contentShape(Rectangle())
                .padding(.vertical, 8)
            }
            .buttonStyle(.plain)

            if expandedShift == shift.id {
                // Nested behind a rail in the shift's own tone, so the
                // boundary between one shift's detail and the next shift's
                // title is visible rather than a hairline.
                shiftDetail(shift)
                    .padding(.leading, 12)
                    .overlay(alignment: .leading) {
                        Rectangle()
                            .fill(toneFor(shift.score ?? 0).opacity(0.4))
                            .frame(width: 2)
                    }
                    .padding(.leading, 4)
                    .padding(.bottom, 10)
                    .transition(.opacity)
            }
            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
        }
    }

    /// Why this shift scored what it did — chosen people, what held it back,
    /// and anything the engine could not see. Kept to what a manager would
    /// read standing at the pass.
    private func shiftDetail(_ shift: QualityShift) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let people = shift.people, !people.isEmpty {
                Text("On this shift: " + people.joined(separator: ", "))
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Judged as \(shift.profile.label)"
                 + (shift.profile.minQuality.map { ", which wants \($0)+" } ?? "") + ".")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            // Grouped under quiet labels rather than eight identically
            // marked lines — the same list read as a wall of text.
            // The dimension that caps the shift decides its number, so its
            // weakness leads, under a line that says so.
            let capLines = cappingLines(shift)
            if !capLines.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    HomeMixedText.make("This is what's holding the shift at \(shift.score ?? 0)",
                                       size: 13.5, weight: 700, color: .cavnarAmber)
                    ForEach(capLines.prefix(2), id: \.self) { line in
                        detailLine(line, symbol: "circle.fill", color: .cavnarAmber)
                    }
                }
                .padding(.top, 2)
            }
            group("Working well", shift.strengths, limit: 3, color: .cavnarGreen)
            if capLines.isEmpty {
                group("Holding it back", shift.weaknesses, limit: 4, color: .cavnarAmber)
            } else {
                group("Also holding it back", (shift.weaknesses ?? []).filter { !capLines.contains($0) },
                      limit: 4, color: .cavnarAmber)
            }
            group("Not known", shift.blindSpots, limit: 2, color: .cavnarInk3)
            if let failures = shift.failed, !failures.isEmpty {
                group("Could not be worked out",
                      [failures.map(\.label).joined(separator: ", ")
                       + " — left out of this score. This is a fault on our side, not a setting."],
                      limit: 1, color: .cavnarRed)
            }
            // Anything true of the whole week was hoisted into the summary,
            // so a shift with nothing left simply ran like the rest of it.
            if shift.nothingSpecific == true {
                Text("Nothing specific to this shift — it ran like the rest of the week. See \"Why this schedule?\" below.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let capped = shift.cappedBy {
                Text("Capped by \(capped.replacingOccurrences(of: "_", with: " ")) — a shift is never better than its weakest critical part.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let dimensions = shift.dimensions?.filter({ $0.isCustomerFacing }), !dimensions.isEmpty {
                HStack(spacing: 6) {
                    ForEach(dimensions) { d in
                        VStack(spacing: 2) {
                            Text("\(d.score)")
                                .font(.cavnarNumber(14, weight: 700))
                                .foregroundStyle(toneFor(d.score))
                            Text(shortLabel(d.label))
                                .font(.cavnarBody(11))
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
            if let viewModel, !viewModel.whatIfCandidates(for: shift).isEmpty {
                whatIfRow(shift, viewModel: viewModel)
            }
        }
    }

    /// The capping dimension's own weaknesses, from the shift's dimensions.
    private func cappingLines(_ shift: QualityShift) -> [String] {
        guard let key = shift.cappedBy,
              let dim = shift.dimensions?.first(where: { $0.key == key }) else { return [] }
        return dim.weaknesses ?? []
    }

    // MARK: What if

    /// "What if <person> works this shift?" — the week with that one change,
    /// scored by the same endpoint Save uses, with save off. Nothing is
    /// stored until "Put them on".
    private func whatIfRow(_ shift: QualityShift, viewModel: LaborViewModel) -> some View {
        let candidates = viewModel.whatIfCandidates(for: shift)
        let onShift = viewModel.rows(for: shift)
        let who = whatIfWho[shift.id] ?? candidates.first ?? ""
        let forRow = whatIfFor[shift.id]
        let forName = onShift.first { $0.id == forRow }?.employee
        return VStack(alignment: .leading, spacing: 8) {
            Text("WHAT IF")
                .font(.cavnarBody(12, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarEmber2)
            HStack(spacing: 8) {
                Menu {
                    ForEach(candidates, id: \.self) { name in
                        Button(name) { whatIfWho[shift.id] = name; whatIfAnswer[shift.id] = nil }
                    }
                } label: { whatIfMenuLabel(who.isEmpty ? "Pick somebody" : who) }
                Menu {
                    Button("Added to the shift") { whatIfFor[shift.id] = nil; whatIfAnswer[shift.id] = nil }
                    ForEach(onShift) { row in
                        Button("Instead of \(row.employee ?? "")\(row.role.map { " (\($0))" } ?? "")") {
                            whatIfFor[shift.id] = row.id; whatIfAnswer[shift.id] = nil
                        }
                    }
                } label: { whatIfMenuLabel(forName.map { "instead of \($0)" } ?? "added") }
            }
            Button {
                Haptic.light()
                Task { await runWhatIf(shift, who: who, replacing: forRow, viewModel: viewModel) }
            } label: {
                Group {
                    if whatIfBusy == shift.id {
                        CavnarShimmerText(text: "Scoring…")
                    } else {
                        Text("What if \(who.isEmpty ? "they" : who) works this shift?")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(who.isEmpty || whatIfBusy != nil)
            if let answer = whatIfAnswer[shift.id] {
                HomeMixedText.make(answer.text, size: 14, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                if answer.applicable {
                    Button {
                        Haptic.medium()
                        let a = answer
                        whatIfAnswer[shift.id] = nil
                        Task {
                            guard let rows = viewModel.whatIfRows(shift: shift, who: a.who, replacing: a.replacing),
                                  !rows.isEmpty else { return }
                            await viewModel.applyWhatIf(rows, who: a.who, date: a.date)
                        }
                    } label: {
                        Text("Put \(answer.who) on")
                            .font(.cavnarBody(14, weight: 700))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(.top, 6)
    }

    private func whatIfMenuLabel(_ text: String) -> some View {
        HStack(spacing: 4) {
            Text(text)
                .font(.cavnarBody(13.5, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(1)
            Image(systemName: "chevron.up.chevron.down")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Color.cavnarInk3)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
            .fill(Color.cavnarPaper3.opacity(0.5)))
    }

    private func runWhatIf(_ shift: QualityShift, who: String, replacing: String?, viewModel: LaborViewModel) async {
        guard !who.isEmpty, let rows = viewModel.whatIfRows(shift: shift, who: who, replacing: replacing) else { return }
        whatIfBusy = shift.id
        defer { whatIfBusy = nil }
        guard let live = await viewModel.liveScore(rows: rows) else {
            whatIfAnswer[shift.id] = WhatIfAnswer(text: "Couldn't score that just now.", who: who, date: shift.date,
                                                  replacing: replacing, applicable: false)
            return
        }
        let after = live.quality.shifts?.first { $0.date == shift.date && $0.daypart == shift.daypart }?.score
        var text = "This shift \(shift.score ?? 0) → \(after.map(String.init) ?? "—")"
        if let after { text += " (\(ScoreDeltaChip.signed(after - (shift.score ?? 0))))" }
        if let week = live.quality.score, let now = quality.score {
            text += ", the week \(now) → \(week) (\(ScoreDeltaChip.signed(week - now)))"
        }
        text += ". Nothing saved."
        if live.hardRules > 0 { text += " It breaks \(live.hardRules) hard \(live.hardRules == 1 ? "rule" : "rules")." }
        whatIfAnswer[shift.id] = WhatIfAnswer(text: text, who: who, date: shift.date,
                                              replacing: replacing, applicable: true)
    }

    @ViewBuilder
    private func group(_ label: String, _ lines: [String]?, limit: Int, color: Color) -> some View {
        if let lines, !lines.isEmpty {
            VStack(alignment: .leading, spacing: 5) {
                Text(label.uppercased())
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.1)
                    .foregroundStyle(color)
                ForEach(lines.prefix(limit), id: \.self) { line in
                    detailLine(line, symbol: "circle.fill", color: color)
                }
            }
            .padding(.top, 2)
        }
    }

    private func detailLine(_ text: String, symbol: String, color: Color) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol)
                .font(.system(size: symbol == "circle.fill" ? 5 : 10, weight: .bold))
                .foregroundStyle(color)
                .frame(width: 13)
                .padding(.top, symbol == "circle.fill" ? 7 : 4)
            Text(text)
                .font(.cavnarBody(14))
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
                        .font(.cavnarBody(15, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 12, weight: .semibold))
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
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                        ForEach((whatIf.swaps ?? []).prefix(3)) { swap in
                            detailLine(swap.reason, symbol: "arrow.left.arrow.right",
                                       color: .cavnarBlue)
                        }
                    }
                    group("Across the week", quality.strengths, limit: 3, color: .cavnarGreen)
                    group("Worth a look", quality.weaknesses, limit: 3, color: .cavnarAmber)
                    Text("Every line here is measured from the finished schedule, not written by the AI. It explains why each shift scored what it did. It does not record why the AI chose one person over another.")
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 2)
                    if let confidence = quality.confidence, !confidence.reasons.isEmpty {
                        Text(confidence.summary)
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarInk2)
                            .padding(.top, 2)
                        ForEach(confidence.reasons, id: \.self) { reason in
                            detailLine(reason, symbol: "circle.fill", color: .cavnarInk3)
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

    /// The one confidence colour map (J15): high green, moderate the same
    /// as medium (ink2), low amber — a confidence is never red.
    private func confidenceTone(_ level: String) -> Color {
        ConfidenceDisplay.tone(band: level).color
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
                .stroke(Color.cavnarPaper3, lineWidth: 8)
            Circle()
                .trim(from: 0, to: CGFloat(max(0, min(100, score))) / 100)
                .stroke(
                    AngularGradient(
                        colors: [tone.opacity(0.55), tone, tone.opacity(0.9)],
                        center: .center, startAngle: .degrees(0), endAngle: .degrees(360)),
                    style: StrokeStyle(lineWidth: 8, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .shadow(color: tone.opacity(0.5), radius: 6)
            VStack(spacing: -2) {
                Text("\(score)")
                    .font(.cavnarNumber(30, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text("/100")
                    .font(.cavnarNumber(11.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(width: 92, height: 92)
        .opacity(muted ? 0.45 : 1)
        .animation(.easeOut(duration: 0.45), value: score)
    }
}

/// One dimension's bar. Gradient rather than a flat fill so a 91 and a 62
/// are different at a glance and not only by length.
private struct QualityBar: View {
    let score: Int
    let tone: Color
    var height: CGFloat = 8

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.cavnarPaper3.opacity(0.8))
                Capsule()
                    .fill(LinearGradient(colors: [tone.opacity(0.55), tone],
                                         startPoint: .leading, endPoint: .trailing))
                    .frame(width: max(4, geo.size.width * CGFloat(max(0, min(100, score))) / 100))
                    .frame(height: height)
            }
        }
        .frame(height: height)
        .animation(.easeOut(duration: 0.4), value: score)
    }
}

/// The points the score moved on the last edit — "+3" green, "−2" red.
struct ScoreDeltaChip: View {
    let delta: Int

    static func signed(_ n: Int) -> String { n > 0 ? "+\(n)" : (n < 0 ? "−\(-n)" : "±0") }

    var body: some View {
        let tone: Color = delta > 0 ? .cavnarGreen : (delta < 0 ? .cavnarRed : .cavnarInk3)
        Text(Self.signed(delta))
            .font(.cavnarNumber(13.5, weight: 700))
            .foregroundStyle(tone)
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(Capsule().fill(tone.opacity(0.14)))
            .accessibilityLabel(delta >= 0 ? "Up \(delta) points" : "Down \(-delta) points")
    }
}

enum CavnarQualityFormat {
    /// "38" or "37.5" — hours as a manager says them.
    static func hours(_ h: Double) -> String {
        h == h.rounded() ? String(Int(h)) : String(format: "%.1f", h)
    }
}
