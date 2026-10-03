import SwiftUI

/// Time-off requests — asked for by staff in the portal, decided here.
/// An approved range is a hard constraint on the next schedule draft
/// (time_off.py), which is why the decision belongs next to the
/// availability manager rather than in a message thread.
struct TimeOffSection: View {
    @Bindable var viewModel: LaborViewModel
    var onExpand: (() -> Void)? = nil

    var body: some View {
        CavnarDropdown(
            title: "Time off",
            subtitle: viewModel.timeOffPending == 0
                ? (viewModel.timeOff.isEmpty ? "Asked for in the staff portal" : "Nothing waiting")
                : "\(viewModel.timeOffPending) waiting for an answer",
            badge: viewModel.timeOffPending > 0 ? viewModel.timeOffPending : nil,
            isExpanded: $viewModel.timeOffExpanded,
            onExpand: onExpand
        ) {
            VStack(alignment: .leading, spacing: 12) {
                Text("An approved range is kept off the next schedule draft. The employee sees your answer in the portal.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                if viewModel.timeOff.isEmpty {
                    Text("No requests yet.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .italic()
                } else {
                    VStack(spacing: 8) {
                        ForEach(viewModel.timeOff) { req in
                            row(req)
                        }
                    }
                }
                if let warning = viewModel.timeOffWarning {
                    HomeMixedText.make(warning, size: 14, color: .cavnarEmber)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let error = viewModel.timeOffError {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                }
            }
        }
    }

    private func row(_ req: TimeOffRequest) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(req.employeeName).font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk)
                    HomeMixedText.make(req.dateLabel + (req.reason.map { " · \($0)" } ?? ""),
                                       size: 13.5, weight: 500, color: .cavnarInk3)
                }
                Spacer(minLength: 6)
                if req.status != "pending" {
                    Text(req.status == "approved" ? "Approved" : "Not approved")
                        .font(.cavnarBody(12.5, weight: 700))
                        .foregroundStyle(req.status == "approved" ? Color.cavnarGreen : Color.cavnarInk3)
                }
            }
            if req.status == "pending" {
                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        Task { await viewModel.decideTimeOff(req.id, approve: false) }
                    } label: { Text("Deny").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(viewModel.timeOffBusyId == req.id)
                    Button {
                        Haptic.light()
                        Task { await viewModel.decideTimeOff(req.id, approve: true) }
                    } label: {
                        Group {
                            // The pulse, never "…" (DESIGN_SYSTEM §10).
                            if viewModel.timeOffBusyId == req.id { CavnarShimmerText(text: "Approving", color: .white) }
                            else { Text("Approve") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.timeOffBusyId == req.id))
                    .disabled(viewModel.timeOffBusyId == req.id)
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.03))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

struct TimeOffRequest: Decodable, Identifiable, Equatable {
    let id: Int
    let employeeName: String
    let startDate: String
    let endDate: String
    let reason: String?
    let status: String
    let decisionNote: String?
    /// "10/7/26, until 4:00pm" — the dates and, for part of a day, which
    /// part (schedule audit 10/3/26 D-39). Absent on an older server.
    var spanLabel: String? = nil

    enum CodingKeys: String, CodingKey {
        case id, reason, status
        case employeeName = "employee_name"
        case startDate = "start_date"
        case endDate = "end_date"
        case decisionNote = "decision_note"
        case spanLabel = "span_label"
    }

    /// What the manager is deciding: the server's span with the part of
    /// the day, else the dates alone.
    var dateLabel: String { spanLabel.flatMap { $0.isEmpty ? nil : $0 } ?? CavnarDate.mdyRange(startDate, endDate) }
}
