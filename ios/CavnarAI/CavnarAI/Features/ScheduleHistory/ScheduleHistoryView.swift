import SwiftUI

/// Every schedule the restaurant has ever generated, reachable from the
/// Account tab — a durable record independent of the Labor tab's own
/// client-side caching, so a generated schedule is never actually lost
/// even if a view-state bug hides it there.
struct ScheduleHistoryView: View {
    @State private var viewModel = ScheduleHistoryViewModel()

    private static let isoDayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
    private static let displayDayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMM d"
        return f
    }()
    // Parses generated_at as UTC (SQLite's datetime('now') is UTC) — the
    // display formatter below deliberately has no .timeZone set, so it
    // renders in the device's own local time.
    private static let generatedAtParser: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        f.timeZone = TimeZone(identifier: "UTC")
        return f
    }()
    private static let generatedAtDisplayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMM d, h:mm a"
        return f
    }()

    var body: some View {
        Group {
            if viewModel.history.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                ContentUnavailableView("No schedules generated yet", systemImage: "calendar.badge.clock")
            } else if viewModel.isLoading && viewModel.history.isEmpty {
                ProgressView()
            } else if let error = viewModel.errorMessage {
                VStack(spacing: 8) {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    Button("Retry") { Task { await viewModel.load() } }
                }
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(viewModel.history) { entry in
                            NavigationLink {
                                ScheduleHistoryDetailView(historyId: entry.id, weekLabel: weekLabel(entry))
                            } label: {
                                row(entry)
                            }
                            .foregroundStyle(Color.cavnarInk)
                        }
                    }
                    .padding(20)
                }
            }
        }
        .background(Color.cavnarPaper)
        .navigationTitle("Schedule History")
        .navigationBarTitleDisplayMode(.inline)
        .task { await viewModel.load() }
        .refreshable { await viewModel.load() }
    }

    private func weekLabel(_ entry: ScheduleHistoryEntry) -> String {
        guard let start = entry.weekStart, let end = entry.weekEnd,
              let startDate = Self.isoDayFormatter.date(from: start),
              let endDate = Self.isoDayFormatter.date(from: end) else {
            return "Generated schedule"
        }
        return "\(Self.displayDayFormatter.string(from: startDate)) – \(Self.displayDayFormatter.string(from: endDate))"
    }

    @ViewBuilder
    private func row(_ entry: ScheduleHistoryEntry) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(weekLabel(entry))
                    .font(.cavnarBody(14, weight: 700))
                if let generated = Self.generatedAtParser.date(from: entry.generatedAt) {
                    Text("Generated \(Self.generatedAtDisplayFormatter.string(from: generated))")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer()
            if let scheduled = entry.hoursScheduled {
                Text("\(String(format: "%.0f", scheduled))h")
                    .font(.cavnarNumber(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk2)
            }
            Image(systemName: "chevron.right").font(.system(size: 12)).foregroundStyle(Color.cavnarInk3)
        }
        .cavnarCard()
    }
}
