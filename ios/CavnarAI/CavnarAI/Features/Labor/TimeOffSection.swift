import SwiftUI

/// Time off already decided — the history (iOS readability round, 10/8/26).
/// A pending request is decided in Waiting on you, the one list of what
/// staff are waiting on; this dropdown holds what was answered. An
/// approved range is a hard constraint on the next draft (time_off.py).
struct TimeOffSection: View {
    @Bindable var viewModel: LaborViewModel
    var onExpand: (() -> Void)? = nil

    private var decided: [TimeOffRequest] { viewModel.timeOff.filter { $0.status != "pending" } }

    var body: some View {
        CavnarDropdown(
            title: "Time off history",
            subtitle: viewModel.timeOffPending > 0
                ? "\(viewModel.timeOffPending) waiting \u{2014} under Needs you"
                : (decided.isEmpty ? "Asked for in the staff app" : "\(decided.count) answered"),
            isExpanded: $viewModel.timeOffExpanded,
            onExpand: onExpand
        ) {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if decided.isEmpty {
                    Text("Nothing answered yet.")
                        .cavnarText(.secondary)
                } else {
                    VStack(spacing: CavnarSpace.xs) {
                        ForEach(decided) { req in
                            row(req)
                        }
                    }
                }
                if let warning = viewModel.timeOffWarning {
                    CavnarMixedText(warning, role: .secondary, color: .cavnarEmber2)
                }
                if let error = viewModel.timeOffError {
                    Text(error).cavnarText(.secondary, color: .cavnarRedText)
                }
            }
        }
    }

    private func row(_ req: TimeOffRequest) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: 2) {
                Text(req.employeeName).cavnarText(.label)
                CavnarMixedText(req.dateLabel + (req.reason.map { " · \($0)" } ?? ""), role: .secondary)
            }
            Spacer(minLength: CavnarSpace.xs)
            Text(req.status == "approved" ? "Approved" : "Not approved")
                .cavnarText(.label, color: req.status == "approved" ? .cavnarGreen : .cavnarInk2)
        }
        .padding(CavnarSpace.s)
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
