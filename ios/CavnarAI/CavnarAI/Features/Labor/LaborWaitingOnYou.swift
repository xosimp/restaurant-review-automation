import SwiftUI

/// WAITING ON YOU — the requests staff are waiting on, directly under the
/// Labor hero, answered in place (Friction audit #18, U3-9). The same rows
/// and the same decide routes as the Time off and Shift requests sections
/// below; this block only brings the pending ones to the top so a manager
/// on the floor never scrolls past charts to say yes. Hidden when nothing
/// is waiting.
///
/// As on the web (9/25/26 parity): a drafted week staff don't have yet is a
/// row here too — its reach and rule warnings from publish-check, and one
/// tap to send when there is nothing to read first — and approving time off
/// over days the draft puts that person on offers "Redo these days".
struct LaborWaitingOnYou: View {
    @Bindable var viewModel: LaborViewModel
    @Bindable var setupViewModel: ScheduleSetupViewModel
    /// Opens the full Shift requests section (to name who covers a drop).
    var onOpenRequests: () -> Void
    /// Opens the send sheet on a drafted week (its blockers and contacts).
    var onOpenDraft: (Int) -> Void = { _ in }
    /// Opens the drafted week in the editor (iOS parity #5) — Review it.
    var onOpenWeek: (Int) -> Void = { _ in }
    @State private var person: PersonSheetTarget?
    /// The one-tap send, asked first (an outward send is never one tap).
    @State private var confirmingSend: (id: Int, label: String)?
    /// A dropped shift whose cover is being named (ReplacementPickerSheet).
    @State private var choosingCover: ShiftRequest?
    /// A Deny, asked first: the person is told, and there is no undo
    /// (re-audit 10/8/26 M8).
    @State private var confirmingDeny: PendingDeny?

    struct PendingDeny: Identifiable {
        let id: String
        let title: String
        let run: () async -> Void
    }

    private var pendingTimeOff: [TimeOffRequest] { viewModel.timeOff.filter { $0.status == "pending" } }
    private var pendingShifts: [ShiftRequest] { setupViewModel.pendingRequests }

    static func count(timeOff: [TimeOffRequest], shifts: [ShiftRequest],
                      draft: Bool = false, redo: Bool = false) -> Int {
        timeOff.filter { $0.status == "pending" }.count + shifts.filter { $0.status == "pending" }.count
            + (draft ? 1 : 0) + (redo ? 1 : 0)
    }

    var body: some View {
        let total = Self.count(timeOff: viewModel.timeOff, shifts: setupViewModel.pendingRequests,
                               draft: viewModel.draftCheck != nil, redo: viewModel.redoOffer != nil)
        if total > 0 || viewModel.draftSendNote != nil {
            // No kicker of its own: the screen's NEEDS YOU header names the
            // group and carries the count (M14 — it said it twice).
            VStack(alignment: .leading, spacing: 12) {
                if let offer = viewModel.redoOffer { redoRow(offer) }
                if let draft = viewModel.draftCheck, let id = draft.scheduleId { draftRow(draft, id: id) }
                if let note = viewModel.draftSendNote {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: viewModel.draftQueuedActionId != nil ? "clock" : "checkmark")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(Color.cavnarGreen)
                        HomeMixedText.make(note, size: CavnarType.secondary, weight: 600, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        // The send window's Undo, in place (L7) — the note
                        // used to send the owner to Home's strip for it.
                        if viewModel.draftQueuedActionId != nil {
                            Button {
                                Haptic.light()
                                Task { await viewModel.undoDraftSend() }
                            } label: {
                                Group {
                                    if viewModel.isUndoingDraftSend {
                                        CavnarShimmerText(text: "Stopping\u{2026}")
                                    } else {
                                        Text("Undo").cavnarText(.label, color: .cavnarEmber2)
                                    }
                                }
                                .cavnarHitTarget()
                            }
                            .buttonStyle(.plain)
                            .disabled(viewModel.isUndoingDraftSend)
                        }
                    }
                }
                ForEach(pendingTimeOff) { req in timeOffRow(req) }
                ForEach(pendingShifts) { req in shiftRow(req) }
                if let error = viewModel.draftSendError {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let error = viewModel.timeOffError ?? setupViewModel.requestError {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let warning = viewModel.timeOffWarning ?? setupViewModel.requestWarning {
                    HomeMixedText.make(warning, size: CavnarType.secondary, color: .cavnarEmber)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
            .sheet(item: $person) { target in PersonSheet(target: target) }
            .sheet(item: $choosingCover) { req in
                ReplacementPickerSheet(viewModel: setupViewModel, request: req)
            }
            .confirmationDialog(confirmingDeny?.title ?? "Deny this request?",
                                isPresented: Binding(get: { confirmingDeny != nil },
                                                     set: { if !$0 { confirmingDeny = nil } }),
                                titleVisibility: .visible) {
                Button("Deny", role: .destructive) {
                    guard let deny = confirmingDeny else { return }
                    confirmingDeny = nil
                    Task { await deny.run() }
                }
                Button("Keep it pending", role: .cancel) { confirmingDeny = nil }
            } message: {
                Text("They\u{2019}re told it was denied. There\u{2019}s no undo.")
            }
        }
    }

    private func nameButton(_ name: String) -> some View {
        Button {
            Haptic.light()
            person = PersonSheetTarget(key: nil, name: name)
        } label: {
            Text(name)
                .font(.cavnar(.label))
                .foregroundStyle(Color.cavnarInk)
                .underline(false)
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens \(name)'s record")
    }

    /// "Sofia is off — the open draft has them on" · Mon 9/28/26, Tue …
    private func redoRow(_ offer: LaborViewModel.RedoOffer) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("\(offer.name) is off \u{2014} the open draft has them on")
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HomeMixedText.make(offer.dates.map(Self.dayLabel).joined(separator: ", "),
                               size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                Haptic.light()
                Task { await viewModel.redoOfferedDays() }
            } label: {
                Group {
                    if viewModel.isGeneratingSchedule { CavnarShimmerText(text: "Redoing\u{2026}") } else { Text("Redo these days") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isGeneratingSchedule)
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    /// The drafted week: staff don't have it yet, who it reaches, what to
    /// read first; Review it opens the send sheet, and Send goes in one tap
    /// only when the server's unattended check finds nothing to read — no
    /// blocker, soft flag or note (`one_tap_safe`, re-audit 10/8/26 #1;
    /// named by who it REACHES, F2-17).
    private func draftRow(_ draft: PublishCheck, id: Int) -> some View {
        let reach = draft.reach
        let warnings = draft.shown.lines.count
        let notes = draft.notes.count
        var detail = "Staff don\u{2019}t have it yet"
        if let reach, reach.total > 0 { detail += " · reaches \(reach.reachable) of \(reach.total)" }
        if warnings > 0 { detail += " · \(warnings) rule warning\(warnings == 1 ? "" : "s") to read first" }
        if notes > 0 { detail += " · \(notes) note\(notes == 1 ? "" : "s") worth a look" }
        let oneTap = draft.allowsOneTap(scheduleId: id)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                HomeMixedText.make("The week of \(CavnarDate.mdy(draft.weekStart ?? "")) is drafted",
                                   size: CavnarType.body, weight: 700, color: .cavnarInk)
                tag("Schedule")
            }
            HomeMixedText.make(detail, size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    onOpenWeek(id)
                } label: { Text("Review it").frame(maxWidth: .infinity) }
                .buttonStyle(CavnarSecondaryButtonStyle())
                if !oneTap {
                    Button {
                        Haptic.light()
                        onOpenDraft(id)
                    } label: { Text("Send\u{2026}").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                if oneTap {
                    Button {
                        Haptic.light()
                        confirmingSend = (id, (reach?.reachable ?? 0) > 0 ? "Send to \(reach?.reachable ?? 0) staff"
                                                                         : "Publish to the staff portal")
                    } label: {
                        Group {
                            if viewModel.isSendingDraft {
                                CavnarShimmerText(text: "Sending\u{2026}")
                            } else {
                                Text((reach?.reachable ?? 0) > 0 ? "Send to \(reach?.reachable ?? 0) staff"
                                                                 : "Publish to the staff portal")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(viewModel.isSendingDraft)
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .confirmationDialog("Send the week of \(CavnarDate.mdy(draft.weekStart ?? ""))?",
                            isPresented: Binding(get: { confirmingSend != nil }, set: { if !$0 { confirmingSend = nil } }),
                            titleVisibility: .visible) {
            Button(confirmingSend?.label ?? "Send") {
                guard let send = confirmingSend else { return }
                confirmingSend = nil
                Task { if await viewModel.sendDraft(send.id) { onOpenDraft(send.id) } }
            }
            Button("Cancel", role: .cancel) { confirmingSend = nil }
        } message: {
            Text(Self.oneTapConfirmMessage(reach))
        }
    }

    /// What the one-tap confirm promises — only what is known (re-audit
    /// 10/8/26 #13): who it reaches by app, text or email, that the rest
    /// only see the staff portal, and that the gate runs again as it goes.
    /// Never "everyone hears" or "nothing to flag" — the check can change
    /// between this read and the press.
    static func oneTapConfirmMessage(_ reach: PublishReach?) -> String {
        let total = reach?.total ?? 0, reachable = reach?.reachable ?? 0
        var out: String
        if reachable <= 0 {
            out = "Nobody on it can be told by app, text or email \u{2014} it goes to the staff portal only."
        } else if reachable < total {
            out = "\(reachable) of the \(total) on it hear about their shifts by app, text or email; "
                + "the rest only see it in the staff portal."
        } else {
            out = "All \(total) on it hear about their shifts by app, text or email."
        }
        return out + " The publish check runs again as it sends \u{2014} anything new to read stops it."
    }

    /// "Mon 9/28/26" from an ISO date.
    static func dayLabel(_ iso: String) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: String(iso.prefix(10))) else { return CavnarDate.mdy(iso) }
        f.dateFormat = "EEE"
        return f.string(from: d) + " " + CavnarDate.mdy(iso)
    }

    private func timeOffRow(_ req: TimeOffRequest) -> some View {
        let busy = viewModel.timeOffBusyId == req.id
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                nameButton(req.employeeName)
                tag("Time off")
            }
            HomeMixedText.make(req.dateLabel + (req.reason.map { " · \($0)" } ?? ""),
                               size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            decideButtons(busy: busy, denyTitle: "Deny \(req.employeeName)\u{2019}s time off?",
                          deny: { await viewModel.decideTimeOff(req.id, approve: false) },
                          approve: { await viewModel.decideTimeOff(req.id, approve: true) })
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func shiftRow(_ req: ShiftRequest) -> some View {
        let busy = setupViewModel.requestBusyId == req.id
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                if let name = req.employeeName { nameButton(name) } else {
                    Text("Open shift").font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
                }
                tag(req.kindLabel)
            }
            if let swap = req.swapLabel {
                HomeMixedText.make(swap, size: CavnarType.secondary, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HomeMixedText.make(req.whenLabel + (req.reason.map { " · \($0)" } ?? ""),
                               size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            // Only a login the decide route allows (SCHEDULE_DRAFT, the
            // list's can_decide) is offered the buttons.
            if setupViewModel.canDecideShifts {
                decideButtons(busy: busy,
                              denyTitle: "Deny \(req.employeeName.map { "\($0)\u{2019}s" } ?? "this") \(req.kindLabel.lowercased())?",
                              deny: { await setupViewModel.decideShiftRequest(req.id, approve: false) },
                              approve: { await setupViewModel.decideShiftRequest(req.id, approve: true) })
            }
            if !req.isSwap && setupViewModel.canDecideShifts
                && !setupViewModel.activeNames.filter({ $0 != req.employeeName }).isEmpty {
                // Approve opens a drop for anyone to claim; this names who
                // covers it outright (the replacement picker).
                Button {
                    Haptic.light()
                    choosingCover = req
                } label: {
                    Text("Name who covers")
                        .cavnarText(.label, color: .cavnarEmber2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .disabled(busy)
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func tag(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(CavnarType.tag, weight: 700))
            .tracking(0.5)
            .foregroundStyle(Color.cavnarInk2)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(Capsule().fill(Color.white.opacity(0.06)))
    }

    /// The decide-in-place pair (re-audit 10/8/26 M8): Approve is the
    /// row's filled primary — the answer the request is asking for — and
    /// Deny is the quiet text action beside it, asked first: the person is
    /// told, and a denial has no undo.
    private func decideButtons(busy: Bool, denyTitle: String, deny: @escaping () async -> Void,
                               approve: @escaping () async -> Void) -> some View {
        HStack(spacing: 10) {
            Button {
                Haptic.light()
                confirmingDeny = PendingDeny(id: denyTitle, title: denyTitle, run: deny)
            } label: {
                Text("Deny")
                    .cavnarText(.label, color: .cavnarInk2)
                    .padding(.horizontal, CavnarSpace.xs)
                    .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .disabled(busy)
            Button {
                Haptic.light()
                Task { await approve() }
            } label: {
                Group {
                    if busy { CavnarShimmerText(text: "Deciding…") } else { Text("Approve") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy))
            .disabled(busy)
        }
    }
}

/// The pinned send bar (Friction audit #19, U3-9): once a week is drafted,
/// Send sits at the bottom of the screen instead of under the whole table,
/// with the rules check one tap away. Saving still comes first — staff get
/// the saved week, as on the web.
struct LaborSendBar: View {
    let issues: Int
    let unsaved: Bool
    /// One primary per state (re-audit 10/8/26 M16): with edits no save has
    /// stored, the bar's primary is Save — Send waits for it, as staff get
    /// the saved week — and the review panel's own Save is gone.
    var isSaving = false
    var saveLabel = "Save changes"
    var onSave: () -> Void = {}
    var onReview: () -> Void
    var onSend: () -> Void

    var body: some View {
        VStack(spacing: 6) {
            if unsaved {
                Text("Save your changes before sending — staff get the saved week.")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk2)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(spacing: 10) {
                if issues > 0 {
                    Button {
                        Haptic.light()
                        onReview()
                    } label: {
                        HStack(spacing: 6) {
                            Text("Review")
                            Text("\(issues)").font(.cavnarNumber(CavnarType.secondary, weight: 700))
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                if unsaved {
                    Button {
                        Haptic.medium()
                        onSave()
                    } label: {
                        Group {
                            if isSaving { CavnarShimmerText(text: "Saving\u{2026}") } else { Text(saveLabel) }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: isSaving))
                    .disabled(isSaving)
                    .layoutPriority(1)
                } else {
                    Button {
                        Haptic.light()
                        onSend()
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "paperplane.fill").font(.system(size: 13, weight: .semibold))
                            Text("Send to staff")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .layoutPriority(1)
                }
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 10)
        .padding(.bottom, 8)
        .background(Color.cavnarPaper.opacity(0.96))
        .overlay(alignment: .top) { Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1) }
    }
}
