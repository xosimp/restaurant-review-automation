import SwiftUI

/// The compliance read over a generated week, as the iPhone decides it (iOS
/// readability round, 10/8/26): "2 must fix · 3 worth a look", the top
/// three things to fix — rules that must hold first, each with its fix — and
/// the one Apply fixes. Everything else the read carries (the rest of the
/// list, the review's structured parts, what was swapped, standby, likely
/// edits, how the week was made) is behind "Show all N".
///
/// Every line here is deterministic: the review is computed from the rows,
/// never written by a model, so it can only name a person and a shift
/// that are actually in the week.
struct ScheduleReviewPanel: View {
    @Bindable var viewModel: LaborViewModel
    let result: GeneratedSchedule
    /// A person a review line names (an unmatched name's suggestion).
    var onOpenPerson: (String) -> Void = { _ in }
    /// Account → Profile's hours, for the close times a setup line asks for.
    var onOpenHours: () -> Void = {}

    /// The overtime move whose "Pass" is asking why.
    @State private var decliningMove: OvertimeMove?
    @State private var showingDeclineWhy = false

    private var review: ScheduleReview? { result.review }

    /// How many things lead the panel before "Show all N".
    private static let topCount = 3

    var body: some View {
        let issues = rankedIssues
        let top = Array(issues.prefix(Self.topCount))
        let rest = Array(issues.dropFirst(Self.topCount))
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            header
            if !top.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.s) {
                    ForEach(top) { issueRow($0) }
                }
            }
            actions
            if hasMore(rest: rest) {
                CavnarMoreDisclosure(hiddenCount: max(rest.count, 1),
                                     total: issues.count > Self.topCount ? issues.count : nil) {
                    VStack(alignment: .leading, spacing: CavnarSpace.m) {
                        ForEach(rest) { issueRow($0) }
                        if usesViolations, let lines = review?.lines, !lines.isEmpty { linesBlock(lines) }
                        // The review's structured parts (schedule audit
                        // 10/3/26): stages that didn't run, what the week
                        // doesn't meet, the setup to confirm, names that
                        // match nobody, the hours by pay.
                        ScheduleReviewExtras(viewModel: viewModel, result: result,
                                             onOpenPerson: onOpenPerson, onOpenHours: onOpenHours)
                        if let fixes = review?.fixes, !fixes.isEmpty { fixesBlock(fixes) }
                        if let unfixed = review?.unfixed, !unfixed.isEmpty { unfixedBlock(unfixed) }
                        if !standbyDays.isEmpty { standbyBlock }
                        if !predictedEdits.isEmpty { likelyEditsBlock }
                        provenance
                    }
                }
            }
        }
        .padding(CavnarSpace.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .fill(Color.cavnarPaper2.opacity(0.6)))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .strokeBorder(tone.opacity(0.35), lineWidth: 1))
        .sheet(item: Binding(
            get: { viewModel.saveConflict.map { ConflictBox(conflict: $0) } },
            set: { if $0 == nil { viewModel.saveConflict = nil } })) { box in
            SaveConflictSheet(viewModel: viewModel, conflict: box.conflict)
        }
        .recReasonDialog(isPresented: $showingDeclineWhy) { reason in
            guard let move = decliningMove else { return }
            decliningMove = nil
            Task { await viewModel.declineOvertimeMove(move, reason: reason) }
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
        if review.softCount > 0 { return .cavnarAmber }
        return .cavnarGreen
    }

    /// "Rules check" and the plain count: "2 must fix · 3 worth a look".
    private var header: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Rules check")
            if let review {
                if review.isClean {
                    Text("Every rule holds")
                        .cavnarText(.label, color: .cavnarGreen)
                } else {
                    HStack(spacing: CavnarSpace.xs) {
                        if review.hardCount > 0 {
                            countPill("\(review.hardCount) must fix", tone: .cavnarRedText)
                        }
                        if review.softCount > 0 {
                            countPill("\(review.softCount) worth a look", tone: .cavnarAmber)
                        }
                    }
                }
            } else {
                Text("No rules check on this week")
                    .cavnarText(.secondary)
            }
        }
    }

    private func countPill(_ text: String, tone: Color) -> some View {
        HomeMixedText.make(text, role: .label, color: tone)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(Capsule().fill(tone.opacity(0.14)))
    }

    // MARK: The ranked list

    /// One thing to look at: a rule broken on a row, a server line (older
    /// payloads carry lines only), or overtime with somebody who has room.
    private enum Issue: Identifiable {
        case violation(RuleViolation)
        case line(String)
        case overtime(OvertimeMove)

        var id: String {
            switch self {
            case .violation(let v): return "v-\(v.id)-\(v.date ?? "")-\(v.shiftStart ?? "")"
            case .line(let l): return "l-\(l)"
            case .overtime(let m): return "o-\(m.id)"
            }
        }
    }

    /// The structured violations when the server sent them, else its lines.
    private var usesViolations: Bool { !(result.ruleViolations ?? []).isEmpty }

    /// Must-fix first, then overtime that can be moved, then worth a look.
    private var rankedIssues: [Issue] {
        let moves = viewModel.overtimeMoves.map(Issue.overtime)
        if usesViolations {
            let all = result.ruleViolations ?? []
            let hard = all.filter(\.isHard).map(Issue.violation)
            let soft = all.filter { !$0.isHard }.map(Issue.violation)
            return hard + moves + soft
        }
        let lines = (review?.lines ?? []).map(Issue.line)
        let warns = lines.filter { if case .line(let l) = $0 { return l.hasPrefix("⚠") } else { return false } }
        let others = lines.filter { if case .line(let l) = $0 { return !l.hasPrefix("⚠") } else { return false } }
        return warns + moves + others
    }

    private func hasMore(rest: [Issue]) -> Bool {
        !rest.isEmpty
            || (usesViolations && !(review?.lines ?? []).isEmpty)
            || !(review?.fixes ?? []).isEmpty || !(review?.unfixed ?? []).isEmpty
            || !standbyDays.isEmpty || !predictedEdits.isEmpty
            || review?.stageFailures != nil || review?.unmet != nil || review?.setup != nil
            || review?.unmatchedNames != nil || review?.budgetConflict != nil
    }

    @ViewBuilder
    private func issueRow(_ issue: Issue) -> some View {
        switch issue {
        case .violation(let v): violationRow(v)
        case .line(let line): lineRow(line)
        case .overtime(let move): overtimeRow(move)
        }
    }

    /// A rule the week breaks, in the server's own words — never its key —
    /// with what Cavnar AI can do about it.
    private func violationRow(_ v: RuleViolation) -> some View {
        let label = (v.label?.isEmpty == false ? v.label : nil) ?? "A rule this week breaks"
        let when = [v.employee, CavnarDate.dayDate(v.day ?? v.date.flatMap { LaborViewModel.weekdayName($0) }, v.date),
                    v.shiftStart].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " \u{00B7} ")
        let fix = (review?.fixes ?? []).first { $0.index != nil && $0.index == v.index }
        let stuck = (review?.unfixed ?? []).contains { $0.index != nil && $0.index == v.index }
        return HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Image(systemName: v.isHard ? "exclamationmark.octagon.fill" : "exclamationmark.triangle.fill")
                .font(.cavnar(.secondary))
                .foregroundStyle(v.isHard ? Color.cavnarRed : Color.cavnarAmber)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                CavnarMixedText(label, role: .label, color: .cavnarInk)
                if !when.isEmpty { CavnarMixedText(when, role: .secondary) }
                if let detail = v.detail, !detail.isEmpty { CavnarMixedText(detail, role: .secondary) }
                if let fix, let to = fix.to {
                    CavnarMixedText("Fix: swap in \(to)", role: .secondary, color: .cavnarBlue)
                } else if stuck {
                    Text("Nobody legal is free \u{2014} change this shift in the week below.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func lineRow(_ line: String) -> some View {
        let warns = line.hasPrefix("⚠")
        let text = warns ? String(line.dropFirst()).trimmingCharacters(in: .whitespaces) : line
        return HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Image(systemName: warns ? "exclamationmark.triangle.fill" : "circle.fill")
                .font(warns ? .cavnar(.secondary) : .system(size: 5, weight: .bold))
                .foregroundStyle(warns ? Color.cavnarAmber : Color.cavnarInk3)
                .frame(width: 14)
                .accessibilityHidden(true)
            CavnarMixedText(text, role: warns ? .label : .body, color: warns ? .cavnarInk : nil)
        }
    }

    /// Overtime the week creates, with a same-role person who has room. One
    /// tap moves the shift and saves the week; Pass is its no.
    private func overtimeRow(_ move: OvertimeMove) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarMixedText(moveLine(move), role: .body)
            HStack(spacing: CavnarSpace.m) {
                Button("Move to \(move.candidate.employee)") {
                    Task { await viewModel.applyOvertimeMove(move) }
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.isRescoringQuality)
                if move.showsNotForUs {
                    Button {
                        Haptic.light()
                        decliningMove = move
                        showingDeclineWhy = true
                    } label: {
                        Text("Pass")
                            .cavnarText(.label, color: .cavnarInk2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: Behind "Show all"

    private func linesBlock(_ lines: [String]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("The full read")
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                lineRow(line)
            }
        }
    }

    private func fixesBlock(_ fixes: [ReviewFix]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker(viewModel.hasUnsavedFixes ? "Fixed \u{2014} not saved yet" : "Fixes", tint: .cavnarBlue)
            ForEach(fixes) { fix in
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    Image(systemName: "arrow.left.arrow.right")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarBlue)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(fix.from ?? "—") → \(fix.to ?? "—")")
                            .cavnarText(.label)
                        // A row edited away since (index null, B1-1): the
                        // fix still names its shift.
                        if fix.index == nil, let label = fix.row?.label, !label.isEmpty {
                            CavnarMixedText(label + " \u{00B7} since edited", role: .caption)
                        }
                        if let reason = fix.reason, !reason.isEmpty {
                            CavnarMixedText(reason, role: .secondary)
                        }
                    }
                }
            }
        }
    }

    private var standbyDays: [StandbyDay] { (result.standbyDays ?? []).filter { $0.standby != nil } }

    /// The days likely to lose somebody, each naming who could be on call.
    private var standbyBlock: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Standby")
            // What the % assumes and the base rate it is smoothed toward
            // (I9: each day's `assumption` + `base_rate`, said once).
            if let note = result.standbyNote?.value ?? StandbyDay.basisLine(standbyDays) {
                CavnarMixedText(note, role: .caption)
            }
            ForEach(standbyDays) { day in
                if let person = day.standby {
                    VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                        CavnarMixedText(
                            "\(day.day ?? "") \(CavnarDate.mdy(day.date)): about "
                                + "\(Int(((day.chanceOfANoShow ?? 0) * 100).rounded()))% chance somebody doesn't show. "
                                + "\(person.employee) is off and could be on call.",
                            role: .secondary)
                        // What the % assumes / how it has held up (I9).
                        if let note = day.calibrationText {
                            CavnarMixedText(note, role: .caption)
                        }
                        if let said = viewModel.standbyAsked[day.date] {
                            Text(said).cavnarText(.secondary)
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
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("LIKELY EDITS")
            Text("Rows you'll probably change, from your own past edits.")
                .cavnarText(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            // How often a flag like this has been right here (I9) — the
            // backtest every predicted row carries, stated once.
            if let note = result.likelyEditsNote?.value ?? predictedEdits.lazy.compactMap(\.backtestLine).first {
                CavnarMixedText(note + (note.hasSuffix(".") ? "" : "."), role: .caption)
            }
            ForEach(predictedEdits) { edit in
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                    Text("\(Int(((edit.likelihood ?? 0) * 100).rounded()))%")
                        .cavnarText(.figureS, color: .cavnarEmber2)
                        .frame(width: 46, alignment: .leading)
                    VStack(alignment: .leading, spacing: 2) {
                        CavnarMixedText(likelyEditTitle(edit), role: .label)
                        if let reason = edit.reason, !reason.isEmpty {
                            CavnarMixedText(reason.prefix(1).uppercased() + reason.dropFirst() + ".", role: .secondary)
                        }
                        // The row's own note only when the block above
                        // could not state the backtest once for all rows.
                        if edit.backtestLine == nil, let note = edit.calibrationText {
                            CavnarMixedText(note, role: .caption)
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
        // A move not yet kept: conditional, an estimate (NS3 labor #13).
        if let saves = c.saves, saves > 0 { line += ", about $\(Int(saves.rounded())) less overtime pay if you keep it" }
        return line + "."
    }

    private func unfixedBlock(_ unfixed: [ReviewUnfixed]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Still needs a hand", tint: .cavnarRedText)
            ForEach(unfixed) { item in
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.employee ?? item.index.map { "Row \($0 + 1)" } ?? "A shift")
                        .cavnarText(.label)
                    if let reason = item.reason, !reason.isEmpty {
                        CavnarMixedText(reason, role: .secondary)
                    }
                }
            }
            Text("Nobody legal was free for these. Tap \u{22EF} on the shift in the week to pick someone yourself.")
                .cavnarText(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
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
            HStack(spacing: CavnarSpace.s) {
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
                                Text(viewModel.optimizerUnsaved ? "Save Cavnar AI's changes" : "Save fixes")
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
                        HStack(spacing: CavnarSpace.xxs + 2) {
                            Image(systemName: "sparkles").font(.cavnar(.secondary))
                            Text("Improve with Cavnar AI")
                        }
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isOptimizing || viewModel.isApplyingFixes || viewModel.isRescoringQuality)
            Text("Legal changes that raise the score. Nothing saves until you do.")
                .cavnarText(.caption, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let error = viewModel.optimizeError {
            Text(error)
                .cavnarText(.secondary, color: .cavnarRedText)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let error = viewModel.applyFixesError {
            Text(error)
                .cavnarText(.secondary, color: .cavnarRedText)
                .fixedSize(horizontal: false, vertical: true)
        }
        // "Updated schedule sent to Ana, Bob" — only after a save of a
        // week staff already have.
        if let notice = viewModel.saveNotice {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Image(systemName: "paperplane.fill")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarGreen)
                    .accessibilityHidden(true)
                CavnarMixedText(notice, role: .secondary, color: .cavnarGreen)
            }
        }
    }

    // MARK: How the week was made

    /// How long it took and how many people it drew from.
    @ViewBuilder
    private var provenance: some View {
        let parts: [String] = {
            var out: [String] = []
            if let s = result.generationSeconds, s > 0 {
                out.append(s < 60 ? "Built in \(Int(s.rounded()))s"
                                  : "Built in \(Int((s / 60).rounded(.down)))m \(Int(s.truncatingRemainder(dividingBy: 60)))s")
            }
            if let roster = result.roster, !roster.isEmpty { out.append("\(roster.count) on the roster") }
            return out
        }()
        if !parts.isEmpty {
            CavnarMixedText(parts.joined(separator: " · "), role: .caption)
        }
    }
}

/// Why this person is on this shift — Cavnar AI's sentence and the facts
/// it was built from, as chips. Opens from a tap on a schedule row.
struct AssignmentExplanationSheet: View {
    let row: ScheduleRow
    let explanation: AssignmentExplanation?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        Text(row.employee ?? "")
                            .cavnarText(.headline)
                        CavnarMixedText(
                            [row.day, row.date.map(CavnarDate.mdy),
                             row.role, "\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")"]
                                .compactMap { $0 }.filter { !$0.isEmpty && $0 != "–" }
                                .joined(separator: " · "),
                            role: .secondary)
                    }

                    if row.needsReview == true, let reason = row.reviewReason, !reason.isEmpty {
                        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .font(.cavnar(.secondary))
                                .foregroundStyle(Color.cavnarAmber)
                                .accessibilityHidden(true)
                            CavnarMixedText(reason, role: .label, color: .cavnarAmber)
                        }
                        .padding(CavnarSpace.s)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(Color.cavnarAmber.opacity(0.1)))
                    }

                    VStack(alignment: .leading, spacing: CavnarSpace.s) {
                        CavnarKicker("Why this person")
                        if let why = explanation?.why, !why.isEmpty {
                            CavnarMixedText(why, role: .body, color: .cavnarInk)
                        } else {
                            Text("No facts on file for this assignment.")
                                .cavnarText(.body)
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
                }
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("This shift")
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }
}
