import SwiftUI

/// Shifts staff have asked to hand back, decided here — the same
/// decide-in-place row as Time off (name, when, Deny / Approve), with
/// one more move: approving can name who covers it. Below, the open
/// shifts nobody has claimed yet.
struct ShiftRequestsSection: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    var onExpand: (() -> Void)? = nil

    // The request an Approve is choosing a replacement for.
    @State private var choosingFor: ShiftRequest?

    var body: some View {
        CavnarDropdown(
            title: "Shift requests",
            subtitle: subtitle,
            badge: viewModel.pendingRequests.isEmpty ? nil : viewModel.pendingRequests.count,
            isExpanded: $viewModel.requestsExpanded,
            onExpand: { onExpand?(); Task { await viewModel.loadShiftRequests() } }
        ) {
            VStack(alignment: .leading, spacing: 12) {
                Text("Asked for in the staff portal. Approving opens the shift for anyone to claim; name a replacement to cover it outright.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)

                if viewModel.isLoadingRequests && viewModel.shiftRequests.isEmpty && viewModel.openShifts.isEmpty {
                    CavnarSkeletonLines(widths: [1.0, 0.7])
                } else if viewModel.shiftRequests.isEmpty && viewModel.openShifts.isEmpty {
                    Text("No requests yet.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .italic()
                } else {
                    VStack(spacing: 8) {
                        ForEach(viewModel.shiftRequests) { req in
                            row(req)
                        }
                    }
                    if !viewModel.openShifts.isEmpty {
                        openBlock
                    }
                }

                if let error = viewModel.requestError {
                    Text(error)
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .sheet(item: $choosingFor) { req in
            ReplacementPickerSheet(viewModel: viewModel, request: req)
        }
    }

    private var subtitle: String {
        let pending = viewModel.pendingRequests.count
        let open = viewModel.openShifts.count
        if pending > 0 { return "\(pending) waiting for an answer" }
        if open > 0 { return "\(open) open \(open == 1 ? "shift" : "shifts") unclaimed" }
        return viewModel.shiftRequests.isEmpty ? "Asked for in the staff portal" : "Nothing waiting"
    }

    private func row(_ req: ShiftRequest) -> some View {
        let busy = viewModel.requestBusyId == req.id
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(req.employeeName ?? "Open shift")
                            .font(.cavnarBody(15, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                        kindPill(req)
                    }
                    if let swap = req.swapLabel {
                        // "Ana ↔ Bob: Mon 4:00pm for Wed 4:00pm"
                        HomeMixedText.make(swap, size: 14, weight: 600, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HomeMixedText.make(req.whenLabel + (req.reason.map { " · \($0)" } ?? ""),
                                       size: 13.5, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 6)
                if req.status != "pending" {
                    Text(statusLabel(req.status))
                        .font(.cavnarBody(12.5, weight: 700))
                        .foregroundStyle(statusTone(req.status))
                }
            }
            if req.status == "pending" {
                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        Task { await viewModel.decideShiftRequest(req.id, approve: false) }
                    } label: { Text("Deny").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(busy)
                    Button {
                        Haptic.light()
                        // A swap moves both shifts as asked — there is no
                        // replacement to name. The picker is for drops.
                        if req.isSwap || viewModel.activeNames.filter({ $0 != req.employeeName }).isEmpty {
                            Task { await viewModel.decideShiftRequest(req.id, approve: true) }
                        } else {
                            choosingFor = req
                        }
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
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func kindPill(_ req: ShiftRequest) -> some View {
        Text(req.kindLabel.uppercased())
            .font(.cavnarBody(9.5, weight: 700))
            .tracking(0.5)
            .foregroundStyle(req.isSwap ? Color.cavnarBlue : Color.cavnarInk3)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(Capsule().fill(req.isSwap ? Color.cavnarBlue.opacity(0.14) : Color.white.opacity(0.06)))
    }

    private func statusLabel(_ status: String) -> String {
        switch status {
        case "approved", "covered": return "Covered"
        case "open": return "Open — unclaimed"
        case "denied": return "Not approved"
        case "claimed": return "Claimed"
        case "withdrawn": return "Withdrawn"
        default: return status.prefix(1).uppercased() + status.dropFirst()
        }
    }

    private func statusTone(_ status: String) -> Color {
        switch status {
        case "approved", "covered", "claimed": return .cavnarGreen
        case "open": return .cavnarAmber
        default: return .cavnarInk3
        }
    }

    private var openBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "person.crop.circle.badge.questionmark")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarAmber)
                Text("OPEN SHIFTS")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.1)
                    .foregroundStyle(Color.cavnarAmber)
            }
            Text("Approved and posted to the portal — nobody has claimed these yet.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 6) {
                ForEach(viewModel.openShifts) { shift in
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            HomeMixedText.make(shift.whenLabel, size: 14.5, weight: 600, color: .cavnarInk)
                            if let name = shift.employeeName, !name.isEmpty {
                                Text("was \(name)'s")
                                    .font(.cavnarBody(13))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        Spacer()
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.cavnarAmber.opacity(0.07)))
    }
}

/// Approve, and say who covers it — or leave it open for anyone.
private struct ReplacementPickerSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    let request: ShiftRequest
    @Environment(\.dismiss) private var dismiss

    @State private var replacement: String?

    private var candidates: [String] {
        viewModel.activeNames.filter { $0 != request.employeeName }
    }
    private var busy: Bool { viewModel.requestBusyId == request.id }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(request.employeeName ?? "Open shift")
                            .font(.cavnarHeadline(22))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(request.whenLabel, size: 14.5, color: .cavnarInk3)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text("WHO COVERS IT")
                            .font(.cavnarBody(11.5, weight: 700))
                            .tracking(0.8)
                            .foregroundStyle(Color.cavnarInk3)
                        Text("Same rules as the generator: the server refuses anyone this would put over a limit, and says why.")
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                        AccountFlowLayout(spacing: 6) {
                            ForEach(candidates, id: \.self) { person in
                                Button {
                                    Haptic.selection()
                                    replacement = replacement == person ? nil : person
                                } label: {
                                    AccountChip(text: person, muted: replacement != person)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    if let error = viewModel.requestError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(spacing: 10) {
                        Button {
                            Haptic.medium()
                            Task {
                                await viewModel.decideShiftRequest(request.id, approve: true, replacement: replacement)
                                if viewModel.requestError == nil { dismiss() }
                            }
                        } label: {
                            Group {
                                if busy {
                                    CavnarShimmerText(text: "Approving…")
                                } else {
                                    Text(replacement.map { "Approve — \($0) covers it" } ?? "Approve and leave it open")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy))
                        .disabled(busy)
                        Button { dismiss() } label: { Text("Cancel").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Approve")
        }
        .presentationDetents([.medium, .large])
        .onAppear { viewModel.requestError = nil }
    }
}
