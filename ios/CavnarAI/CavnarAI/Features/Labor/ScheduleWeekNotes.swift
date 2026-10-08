import SwiftUI

/// What the generator did to the week beyond the rows: what it costs,
/// what it trimmed to fit the budget, whose starts it moved along the
/// sales curve, and what it could not see. Sits in the summary card
/// under the PAR banner. Every line is a fact the payload carried; a
/// missing key renders nothing rather than a zero.
struct ScheduleWeekNotes: View {
    let result: GeneratedSchedule
    /// K8 from GET /labor — the generated schedule itself doesn't carry it.
    var demandAccuracy: DemandAccuracy? = nil

    @State private var showingTrimmed = false
    @State private var showingStaggered = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let cost = result.projectedCost, let total = cost.total { costBlock(cost, total: total) }
            // The week's labor % on the basis the server states (schedule
            // re-audit 10/4/26 SQ-4): all-in for the owner, hourly pay for
            // anyone else, and any projected saving on that same basis.
            if let view = result.laborView, let line = view.line {
                caption(line + (view.text.map { ". \($0)" } ?? "."))
                if let saved = view.savings, saved > 0, let recent = view.recentPct {
                    caption("Projection, not yet earned: about $\(saved.commaFormatted) under your last "
                            + "\(view.recentDays ?? 14) days at \(String(format: "%g", recent))% "
                            + (view.isAllIn ? "all-in" : "hourly pay") + ".")
                }
            }
            if let source = result.projectedRevenueSource, !source.isEmpty {
                caption("Revenue basis: \(source).")
            }
            // Which labor target the budget was built against — the owner's
            // goal ("your goal of 26% by 12/31/26") or the setting.
            if let label = Self.targetLine(label: result.laborTargetLabel?.value,
                                           source: result.laborTargetSource?.value) {
                caption(label)
            }
            // How the demand forecast behind that basis has held up (K8).
            if let record = (result.demandAccuracy ?? demandAccuracy)?.sentence {
                caption(record + ".")
            }
            if result.hourlyProfileReady == false {
                notice("No intraday sales captured yet — starts are not staggered by the sales curve.", tone: .cavnarInk3)
            }
            if let through = result.demandDataThrough, through.blind == true {
                notice(through.date.map { "Demand blind since \(CavnarDate.mdy($0)): the forecast has no sales newer than that." }
                       ?? "Demand blind: the forecast has no recent sales to read.", tone: .cavnarAmber)
            }
            if let conflicts = result.patternConflicts?.items, !conflicts.isEmpty { conflictsBlock(conflicts) }
            if let reqs = result.softRequirements?.items, !reqs.isEmpty { softRequirementsBlock(reqs) }
            if !result.trimmedShifts.isEmpty { trimmedBlock }
            if !result.staggeredStarts.isEmpty { staggeredBlock }
            if let departments = result.departments, !departments.isEmpty {
                caption("Generated per department — \(departments.joined(separator: ", ")) — then stitched.")
            }
            if let dates = result.regeneratedDates, !dates.isEmpty {
                caption("Redone: \(dates.map(CavnarDate.mdy).joined(separator: ", ")). The other days were kept.")
            }
        }
        .animation(.easeOut(duration: 0.22), value: showingTrimmed)
        .animation(.easeOut(duration: 0.22), value: showingStaggered)
    }

    // MARK: Cost

    /// Total, overtime hours and the premium — beside the hours, priced at
    /// the stated rates, never blended into the hours figure.
    private func costBlock(_ cost: ProjectedCost, total: Double) -> some View {
        let over = result.overBudgetDollars ?? 0
        return HStack(alignment: .top, spacing: 14) {
            // Hourly pay, against the hourly budget (SQ-4): salaried pay is
            // in neither.
            stat("Hourly pay", value: "$\(total.commaFormatted)",
                 tone: over > 0 ? .cavnarAmber : .cavnarInk)
            if let ot = cost.overtimeHours, ot > 0 {
                stat("Overtime", value: "\(ot.commaFormatted)h", tone: .cavnarAmber)
            }
            if let premium = cost.overtimePremium, premium > 0 {
                stat("OT premium", value: "$\(premium.commaFormatted)", tone: .cavnarAmber)
            }
            if over > 0 {
                stat("Over hourly budget", value: "$\(over.commaFormatted)", tone: .cavnarRed)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .fill(Color.white.opacity(0.03)))
    }

    private func stat(_ label: String, value: String, tone: Color) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1)
                .foregroundStyle(Color.cavnarInk3)
            Text(value)
                .font(.cavnarNumber(17, weight: 700))
                .foregroundStyle(tone)
                .cavnarSensitive()
        }
    }

    // MARK: Trimmed

    private var trimmedBlock: some View {
        let shifts = result.trimmedShifts
        return VStack(alignment: .leading, spacing: 7) {
            Button {
                Haptic.selection()
                showingTrimmed.toggle()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "scissors")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarAmber)
                    HomeMixedText.make(Self.trimSummary(shifts, hours: result.trimmedHours),
                                       size: 14, weight: 700, color: .cavnarInk)
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(showingTrimmed ? Color.cavnarEmber : Color.cavnarInk3)
                        .rotationEffect(.degrees(showingTrimmed ? 180 : 0))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if showingTrimmed {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(shifts) { shift in
                        VStack(alignment: .leading, spacing: 1) {
                            HomeMixedText.make(
                                [shift.employee, shift.role,
                                 [shift.day, shift.shiftStart.map { "\($0)–\(shift.shiftEnd ?? "")" }].compactMap { $0 }.joined(separator: " "),
                                 shift.kind == "cut" ? shift.to.map { "ended at \($0)" } : nil,
                                 shift.hours.map { "\($0.commaFormatted)h" }]
                                    .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "),
                                size: 13.5, weight: 600, color: .cavnarInk)
                            if let reason = shift.reason, !reason.isEmpty {
                                HomeMixedText.make(reason, size: 13, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                .padding(.leading, 12)
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.cavnarAmber.opacity(0.4)).frame(width: 2)
                }
                .transition(.opacity)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .fill(Color.cavnarAmber.opacity(0.07)))
    }

    /// "Trimmed 12h — 3 ended early, 2 removed" (A1-1): a cut ends a shift
    /// early; anything else took it off the week.
    static func trimSummary(_ shifts: [TrimmedShift], hours: Double) -> String {
        let cut = shifts.filter { $0.kind == "cut" }.count
        let removed = shifts.count - cut
        var parts: [String] = []
        if cut > 0 { parts.append("\(cut) ended early") }
        if removed > 0 { parts.append("\(removed) removed") }
        return "Trimmed \(hours.commaFormatted)h \u{2014} " + parts.joined(separator: ", ")
    }

    // MARK: Staggered

    private var staggeredBlock: some View {
        let starts = result.staggeredStarts
        return VStack(alignment: .leading, spacing: 7) {
            Button {
                Haptic.selection()
                showingStaggered.toggle()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "arrow.right.to.line")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarBlue)
                    HomeMixedText.make("\(starts.count) \(starts.count == 1 ? "start" : "starts") staggered along the sales curve",
                                       size: 14, weight: 700, color: .cavnarInk)
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(showingStaggered ? Color.cavnarEmber : Color.cavnarInk3)
                        .rotationEffect(.degrees(showingStaggered ? 180 : 0))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if showingStaggered {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(starts) { start in
                        VStack(alignment: .leading, spacing: 1) {
                            HomeMixedText.make(
                                [start.employee, start.role, start.date.map(CavnarDate.mdy),
                                 "\(start.from ?? "") → \(start.to ?? "")"]
                                    .compactMap { $0 }.filter { !$0.isEmpty && $0 != " → " }.joined(separator: " · "),
                                size: 13.5, weight: 600, color: .cavnarInk)
                            if let reason = start.reason, !reason.isEmpty {
                                HomeMixedText.make(reason, size: 13, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                .padding(.leading, 12)
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.cavnarBlue.opacity(0.4)).frame(width: 2)
                }
                .transition(.opacity)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .fill(Color.cavnarBlue.opacity(0.06)))
    }

    // MARK: Soft requirements and pattern conflicts (memory round, 9/29/26)

    /// "Labor target: your goal of 26% by 12/31/26." — nil with no label.
    static func targetLine(label: String?, source: String?) -> String? {
        guard let label, !label.isEmpty else { return nil }
        let words = label.prefix(1).uppercased() + label.dropFirst()
        return "Labor target: \(words)" + (source == "goal" && !label.lowercased().contains("goal")
                                             ? " (your goal)" : "") + "."
    }

    /// What the reviews diagnosis and the nightly reports asked of this
    /// week — "+1 server Friday dinner" — each marked Applied / Not applied
    /// from the rows themselves, never from what the model says it did.
    private func softRequirementsBlock(_ reqs: [SoftRequirement]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "person.badge.plus")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
                let done = reqs.filter { $0.applied == true }.count
                HomeMixedText.make("Asked of this week \u{2014} \(done) of \(reqs.count) applied",
                                   size: 14, weight: 700, color: .cavnarInk)
            }
            ForEach(reqs) { req in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        HomeMixedText.make(req.text, size: 13.5, weight: 600, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 4)
                        if let applied = req.appliedLabel {
                            AccountChip(text: applied, tint: req.applied == true ? .cavnarGreen : .cavnarAmber)
                        }
                    }
                    HomeMixedText.make([req.sourceLabel, req.confirm.map { "to confirm: " + $0 },
                                        req.expires.map { "until " + CavnarDate.mdy($0) }]
                                        .compactMap { $0 }.joined(separator: " \u{00B7} "),
                                       size: 12.5, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .fill(Color.cavnarEmber.opacity(0.06)))
    }

    /// Pairs two editors pull opposite ways — left out of this draft for
    /// the owner to settle.
    private func conflictsBlock(_ conflicts: [PatternConflict]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(conflicts) { c in
                notice(c.line + " \u{2014} left out of this draft; settle it in the roster.", tone: .cavnarAmber)
            }
        }
    }

    // MARK: Small lines

    private func caption(_ text: String) -> some View {
        HomeMixedText.make(text, size: 12.5, color: .cavnarInk3)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func notice(_ text: String, tone: Color) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: tone == .cavnarAmber ? "exclamationmark.triangle.fill" : "info.circle")
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(tone)
                .padding(.top, 2)
            HomeMixedText.make(text, size: 13.5, weight: 600, color: tone)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// "+6h · +$90 · 2h overtime" — what the edits on screen move against the
/// draft, in the number face; ember when the dollars went up.
struct EditCostReadout: View {
    let cost: EditCostDelta

    var body: some View {
        if let summary = cost.summary {
            HStack(spacing: 7) {
                Image(systemName: "dollarsign.circle")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(tone)
                HomeMixedText.make(summary, size: 14, weight: 700, color: tone)
                Text("vs the draft")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .accessibilityLabel("This edit: \(summary), against the draft")
        }
    }

    private var tone: Color { (cost.dollarsDelta ?? 0) > 0 ? .cavnarEmber : .cavnarInk2 }
}

/// Somebody changed this week after it was opened. Their lines, and the
/// owner's choice: keep their own edits (saved over theirs, as the newest)
/// or reload theirs. Nothing is decided for them, and the edit on screen is
/// never thrown away unasked (schedule re-audit 10/4/26 UI-1).
struct SaveConflictSheet: View {
    @Bindable var viewModel: LaborViewModel
    let conflict: SaveConflict
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("\(conflict.savedBy ?? "Somebody") changed this week first")
                            .font(.cavnarHeadline(22))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(
                            conflict.error ?? "This week changed after you opened it. Your edits are still here \u{2014} keep yours, or reload theirs.",
                            size: 14.5, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                        if let v = conflict.latestVersion {
                            HomeMixedText.make("Now at v\(v)", size: 13, weight: 600, color: .cavnarInk3)
                        }
                    }
                    if let lines = conflict.lines, !lines.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("WHAT THEY CHANGED")
                                .font(.cavnarBody(11.5, weight: 700))
                                .tracking(1.1)
                                .foregroundStyle(Color.cavnarBlue)
                            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                                HStack(alignment: .top, spacing: 7) {
                                    Circle().fill(Color.cavnarBlue).frame(width: 4, height: 4).padding(.top, 7)
                                    HomeMixedText.make(line, size: 14, color: .cavnarInk2)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(Color.cavnarBlue.opacity(0.07)))
                    }
                    VStack(spacing: 10) {
                        if conflict.latestVersion != nil {
                            // Saved against the version this refusal named:
                            // the owner read what it replaces above.
                            Button {
                                Haptic.medium()
                                Task {
                                    await viewModel.keepMineAfterConflict()
                                    dismiss()
                                }
                            } label: { Text("Keep my edits").frame(maxWidth: .infinity) }
                                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isReloadingAfterConflict))
                                .disabled(viewModel.isReloadingAfterConflict)
                        }
                        Button {
                            Haptic.medium()
                            Task {
                                await viewModel.reloadAfterConflict()
                                if viewModel.saveConflict == nil { dismiss() }
                            }
                        } label: {
                            Group {
                                if viewModel.isReloadingAfterConflict {
                                    CavnarShimmerText(text: "Reloading…")
                                } else {
                                    Text("Reload theirs \u{2014} drops your edits")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .disabled(viewModel.isReloadingAfterConflict)
                        Button {
                            viewModel.saveConflict = nil
                            dismiss()
                        } label: { Text("Decide later \u{2014} keep my edits on screen").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    if case .failed(let message) = viewModel.overrideState, viewModel.isReloadingAfterConflict == false,
                       message != conflict.error {
                        Text(message).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Changed elsewhere")
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .interactiveDismissDisabled(viewModel.isReloadingAfterConflict)
    }
}

/// Which week to generate: next week (the default), the one after, or a
/// date inside any week. Sits under the generate button.
struct GenerateWeekPicker: View {
    @Bindable var viewModel: LaborViewModel
    @State private var showingDate = false
    @State private var pickedDate = Calendar.current.date(byAdding: .day, value: 7, to: Date()) ?? Date()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                chip("Next week", on: viewModel.generateWeek == .next) { viewModel.generateWeek = .next }
                chip("The week after", on: viewModel.generateWeek == .weekAfter) { viewModel.generateWeek = .weekAfter }
                chip(dateLabel, on: isDate) {
                    showingDate = true
                }
            }
            if showingDate {
                DatePicker("Week containing", selection: $pickedDate, in: Date()..., displayedComponents: .date)
                    .datePickerStyle(.compact)
                    .font(.cavnarBody(14))
                    .tint(Color.cavnarEmber)
                    .onChange(of: pickedDate) { _, d in viewModel.generateWeek = .date(d) }
                    .onAppear { viewModel.generateWeek = .date(pickedDate) }
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: showingDate)
        .disabled(viewModel.isGeneratingSchedule)
        .opacity(viewModel.isGeneratingSchedule ? 0.6 : 1)
    }

    private var isDate: Bool {
        if case .date = viewModel.generateWeek { return true }
        return false
    }

    private var dateLabel: String {
        if case .date(let d) = viewModel.generateWeek { return "Week of \(CavnarDate.mdy(LaborViewModel.isoDay.string(from: d)))" }
        return "Pick a date"
    }

    private func chip(_ label: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.selection()
            if !on || label == dateLabel { action() }
            if label != dateLabel { showingDate = false }
        } label: {
            Text(label)
                .font(.cavnarBody(13, weight: on ? 700 : 500))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                .lineLimit(1)
                .padding(.horizontal, 10)
                .frame(minHeight: 30)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? Color.cavnarEmber : Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
        }
        .buttonStyle(.plain)
    }
}

/// "Redo selected days" — the row under the generated week. Tick days on
/// their headers; only those are regenerated, the rest are kept.
struct RedoSelectedDaysRow: View {
    @Bindable var viewModel: LaborViewModel

    var body: some View {
        let n = viewModel.selectedRedoDates.count
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(n == 0 ? "Tick a day to redo just that day" : "\(n) \(n == 1 ? "day" : "days") ticked")
                    .font(.cavnarBody(13.5, weight: n == 0 ? 400 : 600))
                    .foregroundStyle(n == 0 ? Color.cavnarInk3 : Color.cavnarInk)
                if n > 0 {
                    HomeMixedText.make(viewModel.selectedRedoDates.sorted().map(CavnarDate.mdy).joined(separator: ", "),
                                       size: 12.5, color: .cavnarInk3)
                }
            }
            Spacer(minLength: 6)
            Button {
                Haptic.medium()
                // Why first: the redo sheet asks the reason (B2, H1-5).
                viewModel.openRedo()
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.clockwise").font(.system(size: 11, weight: .bold))
                    Text("Redo selected days")
                }
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(n == 0 || viewModel.isGeneratingSchedule || viewModel.scheduleResult?.historyId == nil)
            .opacity(n == 0 ? 0.6 : 1)
        }
    }
}
