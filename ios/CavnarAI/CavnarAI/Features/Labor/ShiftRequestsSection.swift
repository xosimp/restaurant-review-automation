import SwiftUI

/// Shift requests already answered, the open board and Post a shift (iOS
/// readability round, 10/8/26). A pending request is decided in Waiting on
/// you — the one list of what staff are waiting on — where Approve can name
/// who covers it (ReplacementPickerSheet); this dropdown is the history,
/// the open shifts nobody has claimed yet, and posting a new one.
struct ShiftRequestsSection: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    var onExpand: (() -> Void)? = nil

    /// Post a shift, offer one, take one off, a swap agreed in person
    /// (iOS parity #25) — each outward move confirmed.
    @State private var posting = false
    @State private var offering: ShiftRequest?
    @State private var cancelling: ShiftRequest?
    @State private var agreeing: ShiftRequest?

    var body: some View {
        CavnarDropdown(
            title: "Shift requests",
            subtitle: subtitle,
            isExpanded: $viewModel.requestsExpanded,
            onExpand: { onExpand?(); Task { await viewModel.loadShiftRequests() } }
        ) {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if viewModel.isLoadingRequests && viewModel.shiftRequests.isEmpty && viewModel.openShifts.isEmpty {
                    CavnarSkeletonLines(widths: [1.0, 0.7])
                } else if answered.isEmpty && viewModel.openShifts.isEmpty {
                    Text("Nothing answered yet.")
                        .cavnarText(.secondary)
                } else {
                    VStack(spacing: CavnarSpace.xs) {
                        ForEach(answered) { req in
                            row(req)
                        }
                    }
                    if !viewModel.openShifts.isEmpty {
                        openBlock
                    }
                }
                if viewModel.canDecideShifts {
                    Button {
                        Haptic.light()
                        posting = true
                    } label: {
                        Label("Post a shift", systemImage: "plus")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                if let notice = viewModel.openShiftNotice {
                    HomeMixedText.make(notice, size: 13.5, weight: 600, color: .cavnarGreen)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let error = viewModel.requestError {
                    Text(error)
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let warning = viewModel.requestWarning {
                    HomeMixedText.make(warning, size: 14, color: .cavnarEmber)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .sheet(isPresented: $posting) { OpenShiftPostSheet(viewModel: viewModel) }
        .sheet(item: $offering) { shift in OpenShiftOfferSheet(viewModel: viewModel, shift: shift) }
        .confirmationDialog(cancelling.map { "Take \($0.whenLabel) off the open board?" } ?? "",
                            isPresented: Binding(get: { cancelling != nil }, set: { if !$0 { cancelling = nil } }),
                            titleVisibility: .visible) {
            Button("Take it off", role: .destructive) {
                if let c = cancelling { Task { await viewModel.cancelOpenShift(c.id) } }
                cancelling = nil
            }
            Button("Cancel", role: .cancel) { cancelling = nil }
        } message: {
            Text("Whoever it was offered to, and the teammates told about it, hear it\u{2019}s gone.")
        }
        .confirmationDialog(agreeing.map { "\($0.targetName ?? "The colleague") agreed in person?" } ?? "",
                            isPresented: Binding(get: { agreeing != nil }, set: { if !$0 { agreeing = nil } }),
                            titleVisibility: .visible) {
            Button("Yes \u{2014} swap the shifts") {
                if let a = agreeing { Task { await viewModel.colleagueAgreed(a.id) } }
                agreeing = nil
            }
            Button("Cancel", role: .cancel) { agreeing = nil }
        } message: {
            Text("For someone who isn\u{2019}t on the app. The approved swap goes ahead and both shifts move.")
        }
    }

    /// Everything but the pending ones, which Waiting on you decides.
    private var answered: [ShiftRequest] { viewModel.shiftRequests.filter { $0.status != "pending" } }

    private var subtitle: String {
        let pending = viewModel.pendingRequests.count
        let open = viewModel.openShifts.count
        var parts: [String] = []
        if open > 0 { parts.append("\(open) open \(open == 1 ? "shift" : "shifts") unclaimed") }
        if pending > 0 { parts.append("\(pending) waiting \u{2014} under Needs you") }
        if parts.isEmpty { return answered.isEmpty ? "Post a shift, or see what was answered" : "\(answered.count) answered" }
        return parts.joined(separator: " \u{00B7} ")
    }

    private func row(_ req: ShiftRequest) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .top, spacing: CavnarSpace.s) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: CavnarSpace.xs) {
                        Text(req.employeeName ?? "Open shift")
                            .cavnarText(.label)
                        kindPill(req)
                    }
                    if let swap = req.swapLabel {
                        // "Ana ↔ Bob: Mon 4:00pm for Wed 4:00pm"
                        CavnarMixedText(swap, role: .secondary, color: .cavnarInk)
                    }
                    CavnarMixedText(req.whenLabel + (req.reason.map { " · \($0)" } ?? ""), role: .secondary)
                }
                Spacer(minLength: CavnarSpace.xs)
                if req.isSwap && req.status == "approved" {
                    Text("Waiting on \(req.targetName ?? "the colleague")")
                        .cavnarText(.label, color: .cavnarAmber)
                } else if req.status != "pending" {
                    Text(statusLabel(req.status))
                        .cavnarText(.label, color: statusTone(req.status))
                }
            }
            if req.isSwap && req.status == "approved" && viewModel.canDecideShifts {
                Button {
                    Haptic.light()
                    agreeing = req
                } label: {
                    Text("They agreed in person")
                        .cavnarText(.label, color: .cavnarEmber2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .disabled(viewModel.requestBusyId == req.id)
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func kindPill(_ req: ShiftRequest) -> some View {
        Text(req.kindLabel.uppercased())
            .font(.cavnarBody(CavnarType.tag, weight: 700))
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
        default: return .cavnarInk2
        }
    }

    private var openBlock: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Open shifts", icon: "person.crop.circle.badge.questionmark", tint: .cavnarAmber)
            VStack(spacing: CavnarSpace.xs) {
                ForEach(viewModel.openShifts) { shift in
                    let asked = viewModel.shiftOffers.filter { $0.requestId == shift.id }.map(\.name)
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        HStack(spacing: CavnarSpace.s) {
                            VStack(alignment: .leading, spacing: 2) {
                                CavnarMixedText(shift.whenLabel, role: .label)
                                Text(((shift.employeeName ?? "").isEmpty ? "extra shift" : "was \(shift.employeeName ?? "")\u{2019}s")
                                     + " \u{00B7} " + (asked.isEmpty ? "nobody has claimed it yet"
                                                       : "offered to \(asked.joined(separator: ", ")) \u{00B7} waiting on an answer"))
                                    .cavnarText(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                        }
                        if viewModel.canDecideShifts {
                            HStack(spacing: CavnarSpace.m) {
                                Button {
                                    Haptic.light()
                                    offering = shift
                                } label: {
                                    Text("Offer it").cavnarText(.label, color: .cavnarEmber2)
                                        .cavnarHitTarget()
                                }
                                .buttonStyle(.plain)
                                Button {
                                    Haptic.light()
                                    cancelling = shift
                                } label: {
                                    Text("Take it off").cavnarText(.label, color: .cavnarInk2)
                                        .cavnarHitTarget()
                                }
                                .buttonStyle(.plain)
                                .disabled(viewModel.requestBusyId == shift.id)
                            }
                        }
                    }
                    .padding(.vertical, 4)
                    .contextMenu {
                        if viewModel.canDecideShifts {
                            Button { offering = shift } label: { Label("Offer it to someone", systemImage: "person.badge.plus") }
                            Button(role: .destructive) { cancelling = shift } label: { Label("Take it off", systemImage: "xmark") }
                        }
                    }
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

/// Approve, and say who covers it — or leave it open for anyone. Opened
/// from Waiting on you's "Name who covers".
struct ReplacementPickerSheet: View {
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
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        Text(request.employeeName ?? "Open shift")
                            .cavnarText(.headline)
                        CavnarMixedText(request.whenLabel, role: .secondary)
                    }
                    VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                        CavnarKicker("Who covers it")
                        Text("The same rules as Cavnar AI's drafts: anyone this would put over a limit is refused, with why.")
                            .cavnarText(.secondary)
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
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
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
