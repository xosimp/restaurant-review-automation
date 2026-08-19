import SwiftUI

/// One past generation's full detail — the AI's own summary, a PAR-hours
/// check, and every shift grouped by day. Deliberately simpler than the
/// Labor tab's own live scheduleResultSection (no Host/Carry-Out pairing,
/// no inline needs-review flagging): this is a historical record for
/// reference, not the active working view a client edits from.
struct ScheduleHistoryDetailView: View {
    let historyId: Int
    let weekLabel: String
    @State private var viewModel = ScheduleHistoryDetailViewModel()

    var body: some View {
        Group {
            if let detail = viewModel.detail {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if let summary = detail.summary, !summary.isEmpty {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("WHAT CHANGED & WHY")
                                    .font(.cavnarBody(10, weight: 700))
                                    .tracking(1.2)
                                    .foregroundStyle(Color.cavnarGreen)
                                ForEach(summary, id: \.self) { line in
                                    Text("• \(line)")
                                        .font(.cavnarBody(12))
                                        .foregroundStyle(Color.cavnarInk2)
                                        .lineSpacing(5)
                                }
                            }
                            .cavnarCard()
                        }
                        if let budget = detail.hoursBudget, budget > 0, let scheduled = detail.hoursScheduled {
                            parHoursBanner(budget: budget, scheduled: scheduled)
                        }
                        if let rows = detail.previewRows, !rows.isEmpty {
                            scheduleByDay(rows)
                        }
                    }
                    .padding(20)
                }
            } else if viewModel.isLoading {
                ProgressView()
            } else if let error = viewModel.errorMessage {
                VStack(spacing: 8) {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    Button("Retry") { Task { await viewModel.load(id: historyId) } }
                }
            }
        }
        .cavnarModuleBackground()
        .navigationTitle(weekLabel)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // Same ShareLink(item:preview:) pattern as the Labor tab's own
            // schedule export (LaborView.fullScheduleTable) — plain-text
            // CSV content, not a written temp file, matching what already
            // works there rather than introducing a second export
            // mechanism. Ember-tinted icon for the "orange branded" ask.
            if let csv = viewModel.detail?.scheduleCsv {
                ToolbarItem(placement: .topBarTrailing) {
                    ShareLink(item: csv, preview: SharePreview("Schedule — \(weekLabel).csv")) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                }
            }
        }
        .task { await viewModel.load(id: historyId) }
        // Belt-and-suspenders alongside .task — the same fix LaborView
        // needed for its own scheduleResult restore, tied to a signal
        // that isn't dependent on SwiftUI reliably re-running .task for
        // this specific push-from-NavigationLink case. load() itself is a
        // no-op if this id is already loaded/loading, so this costs
        // nothing on the common path where .task already handled it.
        .onAppear { Task { await viewModel.load(id: historyId) } }
    }

    private func parHoursBanner(budget: Double, scheduled: Double) -> some View {
        let diff = scheduled - budget
        let withinRange = abs(diff) <= max(budget * 0.05, 1)
        return HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text("PAR HOURS CHECK")
                    .font(.cavnarBody(9, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarGreen)
                Text("Budgeted \(budget.commaFormatted)h for the week")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk2)
            }
            Spacer()
            Text(withinRange ? "On budget" : (diff > 0 ? "+\(diff.commaFormatted)h over" : "\(diff.commaFormatted)h under"))
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(withinRange ? Color.cavnarGreen : Color.cavnarAmber)
        }
        .padding(10)
        .background(Color.cavnarGreen.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private static let dayOrder = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    @ViewBuilder
    private func scheduleByDay(_ rows: [ScheduleRow]) -> some View {
        let grouped = Dictionary(grouping: rows) { $0.day ?? "" }
        VStack(alignment: .leading, spacing: 14) {
            ForEach(Self.dayOrder.filter { grouped[$0] != nil }, id: \.self) { day in
                VStack(alignment: .leading, spacing: 8) {
                    Text(day.uppercased())
                        .font(.cavnarBody(10, weight: 700))
                        .tracking(1)
                        .foregroundStyle(Color.cavnarInk3)
                    ForEach(grouped[day] ?? []) { row in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(row.employee ?? "—")
                                    .font(.cavnarBody(13, weight: 600))
                                Text(row.role ?? "")
                                    .font(.cavnarBody(11))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            Spacer()
                            Text("\(row.shiftStart ?? "") – \(row.shiftEnd ?? "")")
                                .font(.cavnarBody(12))
                                .foregroundStyle(Color.cavnarInk2)
                        }
                        .padding(.vertical, 4)
                    }
                }
                .cavnarCard()
            }
        }
    }
}
