import SwiftUI

/// WAITING ON YOU — the requests staff are waiting on, directly under the
/// Labor hero, answered in place (Friction audit #18, U3-9). The same rows
/// and the same decide routes as the Time off and Shift requests sections
/// below; this block only brings the pending ones to the top so a manager
/// on the floor never scrolls past charts to say yes. Hidden when nothing
/// is waiting.
struct LaborWaitingOnYou: View {
    @Bindable var viewModel: LaborViewModel
    @Bindable var setupViewModel: ScheduleSetupViewModel
    /// Opens the full Shift requests section (to name who covers a drop).
    var onOpenRequests: () -> Void
    @State private var person: PersonSheetTarget?

    private var pendingTimeOff: [TimeOffRequest] { viewModel.timeOff.filter { $0.status == "pending" } }
    private var pendingShifts: [ShiftRequest] { setupViewModel.pendingRequests }

    static func count(timeOff: [TimeOffRequest], shifts: [ShiftRequest]) -> Int {
        timeOff.filter { $0.status == "pending" }.count + shifts.filter { $0.status == "pending" }.count
    }

    var body: some View {
        let total = pendingTimeOff.count + pendingShifts.count
        if total > 0 {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text("WAITING ON YOU")
                        .font(.cavnarBody(13, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                    Spacer()
                    Text("\(total)")
                        .font(.cavnarNumber(14, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                ForEach(pendingTimeOff) { req in timeOffRow(req) }
                ForEach(pendingShifts) { req in shiftRow(req) }
                if let error = viewModel.timeOffError ?? setupViewModel.requestError {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let warning = viewModel.timeOffWarning {
                    HomeMixedText.make(warning, size: 14, color: .cavnarEmber)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
            .sheet(item: $person) { target in PersonSheet(target: target) }
        }
    }

    private func nameButton(_ name: String) -> some View {
        Button {
            Haptic.light()
            person = PersonSheetTarget(key: nil, name: name)
        } label: {
            Text(name)
                .font(.cavnarBody(15, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .underline(false)
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens \(name)'s record")
    }

    private func timeOffRow(_ req: TimeOffRequest) -> some View {
        let busy = viewModel.timeOffBusyId == req.id
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                nameButton(req.employeeName)
                tag("Time off")
            }
            HomeMixedText.make(req.dateLabel + (req.reason.map { " · \($0)" } ?? ""),
                               size: 13.5, weight: 500, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            decideButtons(busy: busy,
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
                    Text("Open shift").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                }
                tag(req.kindLabel)
            }
            if let swap = req.swapLabel {
                HomeMixedText.make(swap, size: 14, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HomeMixedText.make(req.whenLabel + (req.reason.map { " · \($0)" } ?? ""),
                               size: 13.5, weight: 500, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            decideButtons(busy: busy,
                          deny: { await setupViewModel.decideShiftRequest(req.id, approve: false) },
                          approve: { await setupViewModel.decideShiftRequest(req.id, approve: true) })
            if !req.isSwap {
                // Approving a drop opens it for anyone to claim; naming who
                // covers it outright is the full section's picker.
                Button {
                    Haptic.light()
                    onOpenRequests()
                } label: {
                    Text("Approve opens it for anyone to claim · Name who covers")
                        .font(.cavnarBody(12.5, weight: 600))
                        .foregroundStyle(Color.cavnarEmber2)
                        .multilineTextAlignment(.leading)
                        .frame(minHeight: 44, alignment: .leading)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func tag(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(9.5, weight: 700))
            .tracking(0.5)
            .foregroundStyle(Color.cavnarInk3)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(Capsule().fill(Color.white.opacity(0.06)))
    }

    /// The decide-in-place pair (DESIGN_SYSTEM §12): Deny secondary,
    /// Approve beside it. Secondary here too — the one primary on the
    /// Labor screen stays Send to staff.
    private func decideButtons(busy: Bool, deny: @escaping () async -> Void,
                               approve: @escaping () async -> Void) -> some View {
        HStack(spacing: 10) {
            Button {
                Haptic.light()
                Task { await deny() }
            } label: { Text("Deny").frame(maxWidth: .infinity) }
            .buttonStyle(CavnarSecondaryButtonStyle())
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
            .buttonStyle(CavnarSecondaryButtonStyle())
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
    var onReview: () -> Void
    var onSend: () -> Void

    var body: some View {
        VStack(spacing: 6) {
            if unsaved {
                Text("Save your changes before sending — staff get the saved week.")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
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
                            Text("\(issues)").font(.cavnarNumber(14, weight: 700))
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
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
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: unsaved))
                .disabled(unsaved)
                .layoutPriority(1)
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 10)
        .padding(.bottom, 8)
        .background(Color.cavnarPaper.opacity(0.96))
        .overlay(alignment: .top) { Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1) }
    }
}
