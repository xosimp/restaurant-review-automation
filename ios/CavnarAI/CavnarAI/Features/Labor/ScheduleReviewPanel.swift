import SwiftUI

/// The compliance read over a generated week — the rules it breaks, what
/// the engine could fix by swapping somebody legal in, and what it could
/// not. Sits between the summary and the Shift Quality verdict because a
/// hard violation is decided on before a score is admired.
///
/// Every line here is deterministic: the review is computed from the rows,
/// never written by a model, so it can only name a person and a shift
/// that are actually in the week.
struct ScheduleReviewPanel: View {
    @Bindable var viewModel: LaborViewModel
    let result: GeneratedSchedule

    @State private var showingAllLines = false
    /// The overtime move whose "Not for us" is asking why.
    @State private var decliningMove: OvertimeMove?
    @State private var showingDeclineWhy = false

    private var review: ScheduleReview? { result.review }
    private var pending: [(String, [String])] {
        (result.pendingTimeOff ?? [:]).map { ($0.key, $0.value) }.sorted { $0.0 < $1.0 }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            if let review {
                kindChips
                if let lines = review.lines, !lines.isEmpty { linesBlock(lines) }
                if let fixes = review.fixes, !fixes.isEmpty { fixesBlock(fixes) }
                if let unfixed = review.unfixed, !unfixed.isEmpty { unfixedBlock(unfixed) }
            }
            if !viewModel.overtimeMoves.isEmpty { overtimeBlock }
            if !standbyDays.isEmpty { standbyBlock }
            if !predictedEdits.isEmpty { likelyEditsBlock }
            if !pending.isEmpty { pendingBlock }
            actions
            provenance
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .fill(Color.cavnarPaper2.opacity(0.6)))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .strokeBorder(tone.opacity(0.35), lineWidth: 1))
        .animation(.easeOut(duration: 0.22), value: showingAllLines)
        .sheet(item: Binding(
            get: { viewModel.saveConflict.map { ConflictBox(conflict: $0) } },
            set: { if $0 == nil { viewModel.saveConflict = nil } })) { box in
            SaveConflictSheet(viewModel: viewModel, conflict: box.conflict)
        }
    }

    /// Identifiable wrapper so the 409 can drive `.sheet(item:)`.
    private struct ConflictBox: Identifiable {
        let conflict: SaveConflict
        var id: String { "\(conflict.latestVersion ?? 0)-\(conflict.savedBy ?? "")" }
    }

    // MARK: Header

    private var tone: Color {
        guard let review else { return .cavnarInk3 }
        if review.hardCount > 0 { return .cavnarRed }
        if review.softCount > 0 || !pending.isEmpty { return .cavnarAmber }
        return .cavnarGreen
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("RULES CHECK")
                .font(.cavnarBody(12.5, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarInk3)
            HStack(spacing: 8) {
                if let review {
                    countPill(review.hardCount, word: "hard", tone: review.hardCount > 0 ? .cavnarRed : .cavnarGreen)
                    countPill(review.softCount, word: "soft", tone: review.softCount > 0 ? .cavnarAmber : .cavnarGreen)
                    if review.isClean {
                        Text("Every rule holds")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarGreen)
                    }
                } else {
                    Text("No rules check on this week")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
        }
    }

    private func countPill(_ n: Int, word: String, tone: Color) -> some View {
        HStack(spacing: 5) {
            Text("\(n)")
                .font(.cavnarNumber(14, weight: 700))
            Text(word)
                .font(.cavnarBody(13.5, weight: 700))
        }
        .foregroundStyle(tone)
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .background(Capsule().fill(tone.opacity(0.14)))
        .accessibilityLabel("\(n) \(word) rule \(n == 1 ? "break" : "breaks")")
    }

    // MARK: Kinds

    /// One chip per rule kind the week breaks, in the server's own words
    /// (`label` on each violation) — so a new kind needs no client change.
    @ViewBuilder
    private var kindChips: some View {
        let violations = result.ruleViolations ?? []
        if !violations.isEmpty {
            let grouped = Dictionary(grouping: violations) { $0.kind ?? "" }
            let kinds = grouped.keys.sorted { a, b in
                let ha = grouped[a]?.first?.isHard ?? false, hb = grouped[b]?.first?.isHard ?? false
                return ha != hb ? ha : a < b
            }
            AccountFlowLayout(spacing: 6) {
                ForEach(kinds, id: \.self) { kind in
                    let items = grouped[kind] ?? []
                    let hard = items.first?.isHard ?? false
                    let label = items.first?.label ?? kind.replacingOccurrences(of: "_", with: " ")
                    HStack(spacing: 5) {
                        Text("\(items.count)")
                            .font(.cavnarNumber(12, weight: 700))
                        Text(label)
                            .font(.cavnarBody(12, weight: 600))
                    }
                    .foregroundStyle(hard ? Color.cavnarRed : Color.cavnarAmber)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Capsule().fill((hard ? Color.cavnarRed : Color.cavnarAmber).opacity(0.1)))
                    .accessibilityLabel("\(items.count) \(label), \(hard ? "hard" : "soft")")
                }
            }
        }
    }

    // MARK: Lines

    private func linesBlock(_ lines: [String]) -> some View {
        let shown = showingAllLines ? lines : Array(lines.prefix(5))
        return VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(shown.enumerated()), id: \.offset) { _, line in
                let warns = line.hasPrefix("⚠")
                let text = warns ? String(line.dropFirst()).trimmingCharacters(in: .whitespaces) : line
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: warns ? "exclamationmark.triangle.fill" : "circle.fill")
                        .font(.system(size: warns ? 10 : 5, weight: .bold))
                        .foregroundStyle(warns ? Color.cavnarAmber : Color.cavnarInk3)
                        .frame(width: 13)
                        .padding(.top, warns ? 4 : 7)
                    HomeMixedText.make(text, size: 14, weight: warns ? 600 : 400,
                                       color: warns ? .cavnarAmber : .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if lines.count > 5 {
                Button {
                    Haptic.selection()
                    showingAllLines.toggle()
                } label: {
                    Text(showingAllLines ? "Show fewer" : "Show all \(lines.count)")
                        .font(.cavnarBody(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: Fixes

    private func fixesBlock(_ fixes: [ReviewFix]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(viewModel.hasUnsavedFixes ? "FIXED — NOT SAVED YET" : "FIXES")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarBlue)
            ForEach(fixes) { fix in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "arrow.left.arrow.right")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Color.cavnarBlue)
                        .frame(width: 13)
                        .padding(.top, 4)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(fix.from ?? "—") → \(fix.to ?? "—")")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        if let reason = fix.reason, !reason.isEmpty {
                            HomeMixedText.make(reason, size: 13.5, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
        }
    }

    /// Overtime the week creates, each with a same-role person who has
    /// room. One tap moves the shift and saves the week.
    private var overtimeBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("OVER HOURS YOU CAN MOVE")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarEmber)
            ForEach(viewModel.overtimeMoves) { move in
                VStack(alignment: .leading, spacing: 6) {
                    HomeMixedText.make(moveLine(move), size: 13.5, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 16) {
                        Button("Move to \(move.candidate.employee)") {
                            Task { await viewModel.applyOvertimeMove(move) }
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .disabled(viewModel.isRescoringQuality)
                        // The move is a recommendation: the tap above is
                        // its yes, this is its no (with the one-tap why).
                        if move.showsNotForUs {
                            Button {
                                Haptic.light()
                                decliningMove = move
                                showingDeclineWhy = true
                            } label: {
                                Text("Not for us")
                                    .font(.cavnarBody(12.5, weight: 600))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .recReasonDialog(isPresented: $showingDeclineWhy) { reason in
            guard let move = decliningMove else { return }
            decliningMove = nil
            Task { await viewModel.declineOvertimeMove(move, reason: reason) }
        }
    }

    private var standbyDays: [StandbyDay] { (result.standbyDays ?? []).filter { $0.standby != nil } }

    /// The days likely to lose somebody, each naming who could be on call.
    private var standbyBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("STANDBY")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarInk3)
            // What the % assumes and the base rate it is smoothed toward
            // (I9: each day's `assumption` + `base_rate`, said once).
            if let note = result.standbyNote?.value ?? StandbyDay.basisLine(standbyDays) {
                HomeMixedText.make(note, size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(standbyDays) { day in
                if let person = day.standby {
                    VStack(alignment: .leading, spacing: 6) {
                        HomeMixedText.make(
                            "\(day.day ?? "") \(CavnarDate.mdy(day.date)): about "
                                + "\(Int(((day.chanceOfANoShow ?? 0) * 100).rounded()))% chance somebody doesn't show. "
                                + "\(person.employee) is off and could be on call.",
                            size: 13.5, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                        // What the % assumes / how it has held up (I9).
                        if let note = day.calibrationText {
                            HomeMixedText.make(note, size: 12.5, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let said = viewModel.standbyAsked[day.date] {
                            Text(said)
                                .font(.cavnarBody(13))
                                .foregroundStyle(Color.cavnarInk3)
                        } else {
                            Button("Ask \(person.employee)") {
                                Task { await viewModel.askStandby(day) }
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                    }
                }
            }
        }
    }

    /// Rows the manager's own past edits say they will probably change —
    /// flagged before they see the draft, likeliest first. The matches to
    /// an edit they keep making are already review lines.
    private var predictedEdits: [LikelyEdit] { (result.likelyEdits ?? []).filter { $0.kind == "predicted" } }

    private var likelyEditsBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("LIKELY EDITS")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarEmber)
            Text("From your own past edits — rows you'll probably change. Check these first.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            // How often a flag like this has been right here (I9).
            // (the backtest every predicted row carries — the same on each,
            // so stated once here with the base rate).
            if let note = result.likelyEditsNote?.value ?? predictedEdits.lazy.compactMap(\.backtestLine).first {
                HomeMixedText.make(note + (note.hasSuffix(".") ? "" : "."), size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(predictedEdits) { edit in
                HStack(alignment: .top, spacing: 10) {
                    Text("\(Int(((edit.likelihood ?? 0) * 100).rounded()))%")
                        .font(.cavnarNumber(14, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                        .frame(width: 42, alignment: .leading)
                    VStack(alignment: .leading, spacing: 2) {
                        HomeMixedText.make(likelyEditTitle(edit), size: 14, weight: 600, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        if let reason = edit.reason, !reason.isEmpty {
                            HomeMixedText.make(reason.prefix(1).uppercased() + reason.dropFirst() + ".",
                                               size: 13, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        // The row's own note only when the block above
                        // could not state the backtest once for all rows.
                        if edit.backtestLine == nil, let note = edit.calibrationText {
                            HomeMixedText.make(note, size: 12.5, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func likelyEditTitle(_ e: LikelyEdit) -> String {
        [e.employee, e.date.map { CavnarDate.mdy($0) }, e.shiftStart, e.role]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    private func moveLine(_ move: OvertimeMove) -> String {
        let c = move.candidate
        let over = move.over.map { String(format: "%g", $0) } ?? "?"
        let when = [c.date.map { CavnarDate.mdy($0) }, c.shiftStart].compactMap { $0 }.joined(separator: " ")
        var line = "\(move.employee) is \(over)h over. \(c.employee) has room for the \(when) shift"
        if let saves = c.saves, saves > 0 { line += ", saving about $\(Int(saves.rounded())) in overtime pay" }
        return line + "."
    }

    private func unfixedBlock(_ unfixed: [ReviewUnfixed]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("STILL NEEDS A HAND")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.1)
                .foregroundStyle(Color.cavnarRed)
            ForEach(unfixed) { item in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "person.crop.circle.badge.exclamationmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarRed)
                        .frame(width: 13)
                        .padding(.top, 3)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.employee ?? "Row \(item.index + 1)")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        if let reason = item.reason, !reason.isEmpty {
                            HomeMixedText.make(reason, size: 13.5, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            Text("Nobody legal was free for these. Long-press the row to pick someone yourself.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Pending time off

    private var pendingBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "clock.badge.questionmark")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarAmber)
                Text("TIME OFF NOT DECIDED YET")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.1)
                    .foregroundStyle(Color.cavnarAmber)
            }
            ForEach(pending, id: \.0) { name, dates in
                HomeMixedText.make("\(name) — \(dates.map(CavnarDate.mdy).joined(separator: ", "))",
                                   size: 14, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("A warning, not a block. Answer these under Time off and the next draft will keep them clear.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.cavnarAmber.opacity(0.07)))
    }

    // MARK: Actions

    @ViewBuilder
    private var actions: some View {
        let canFix = (review?.hardCount ?? 0) > 0 && !viewModel.hasUnsavedFixes
        // What the edits on screen cost against the draft — shown by the
        // Save button whenever anything has moved.
        if let cost = viewModel.editCost, cost.summary != nil,
           viewModel.hasUnsavedFixes || !viewModel.overriddenRows.isEmpty {
            EditCostReadout(cost: cost)
        }
        if canFix || viewModel.hasUnsavedFixes {
            HStack(spacing: 10) {
                if canFix {
                    Button {
                        Haptic.medium()
                        Task { await viewModel.applyFixes() }
                    } label: {
                        Group {
                            if viewModel.isApplyingFixes {
                                CavnarShimmerText(text: "Fixing…")
                            } else {
                                Text("Apply fixes")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isApplyingFixes))
                    .disabled(viewModel.isApplyingFixes || viewModel.isRescoringQuality)
                }
                if viewModel.hasUnsavedFixes {
                    Button {
                        Haptic.medium()
                        Task { await viewModel.rescoreQuality(save: true) }
                    } label: {
                        Group {
                            if viewModel.isRescoringQuality {
                                CavnarShimmerText(text: "Saving…")
                            } else {
                                Text(viewModel.optimizerUnsaved ? "Save Cavnar's changes" : "Save fixes")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isRescoringQuality))
                    .disabled(viewModel.isRescoringQuality)
                }
            }
        }
        // The Shift Quality repair loop over the week on screen: legal
        // changes that raise the score, each listed with why in the quality
        // panel. Proposed only — Save fixes is what keeps them.
        if !(result.previewRows ?? []).isEmpty && !viewModel.hasUnsavedFixes {
            Button {
                Haptic.medium()
                Task { await viewModel.optimize() }
            } label: {
                Group {
                    if viewModel.isOptimizing {
                        CavnarShimmerText(text: "Improving…")
                    } else {
                        HStack(spacing: 6) {
                            Image(systemName: "sparkles").font(.system(size: 12, weight: .semibold))
                            Text("Improve with Cavnar")
                        }
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isOptimizing || viewModel.isApplyingFixes || viewModel.isRescoringQuality)
            Text("Makes legal changes that raise the score and lists each one. Nothing is saved until you save.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let error = viewModel.optimizeError {
            Text(error)
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarRed)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let error = viewModel.applyFixesError {
            Text(error)
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarRed)
                .fixedSize(horizontal: false, vertical: true)
        }
        // "Updated schedule sent to Ana, Bob" — only after a save of a
        // week staff already have.
        if let notice = viewModel.saveNotice {
            HStack(alignment: .top, spacing: 7) {
                Image(systemName: "paperplane.fill")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarGreen)
                    .padding(.top, 2)
                HomeMixedText.make(notice, size: 13.5, weight: 600, color: .cavnarGreen)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: Provenance

    /// How the week was made — in how many parts, and how long it took.
    @ViewBuilder
    private var provenance: some View {
        let parts: [String] = {
            var out: [String] = []
            if result.chunked == true { out.append("generated in parts — the roster was too big for one pass") }
            if let s = result.generationSeconds, s > 0 {
                out.append(s < 60 ? "\(Int(s.rounded()))s to generate"
                                  : "\(Int((s / 60).rounded(.down)))m \(Int(s.truncatingRemainder(dividingBy: 60)))s to generate")
            }
            if let roster = result.roster, !roster.isEmpty { out.append("\(roster.count) on the roster") }
            return out
        }()
        if !parts.isEmpty {
            HomeMixedText.make(parts.joined(separator: " · "), size: 12.5, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// Why this person is on this shift — the engine's sentence and the facts
/// it was built from, as chips. Opens from a tap on a schedule row.
struct AssignmentExplanationSheet: View {
    let row: ScheduleRow
    let explanation: AssignmentExplanation?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(row.employee ?? "")
                            .font(.cavnarHeadline(22))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(
                            [row.day, row.date.map(CavnarDate.mdy),
                             row.role, "\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")"]
                                .compactMap { $0 }.filter { !$0.isEmpty && $0 != "–" }
                                .joined(separator: " · "),
                            size: 14.5, color: .cavnarInk3)
                    }

                    if row.needsReview == true, let reason = row.reviewReason, !reason.isEmpty {
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(Color.cavnarAmber)
                                .padding(.top, 3)
                            HomeMixedText.make(reason, size: 14, weight: 600, color: .cavnarAmber)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(Color.cavnarAmber.opacity(0.1)))
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Text("WHY THIS PERSON")
                            .font(.cavnarBody(12.5, weight: 700))
                            .tracking(1.4)
                            .foregroundStyle(Color.cavnarEmber2)
                        if let why = explanation?.why, !why.isEmpty {
                            HomeMixedText.make(why, size: 15, color: .cavnarInk)
                                .lineSpacing(4)
                                .fixedSize(horizontal: false, vertical: true)
                        } else {
                            Text("No facts on file for this assignment.")
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        if let chips = explanation?.factChips, !chips.isEmpty {
                            AccountFlowLayout(spacing: 6) {
                                ForEach(chips, id: \.self) { chip in
                                    AccountChip(text: chip, muted: true)
                                }
                            }
                            .padding(.top, 2)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .cavnarCard(.ai)

                    Text("Measured from the roster, the rules and this week's rows — not written by the model.")
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(20)
            }
            .accountSheetChrome("This shift")
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }
}
