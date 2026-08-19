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
                // List, not a plain ScrollView+ForEach (every other module
                // screen's convention) — .swipeActions only works inside a
                // real List row, and swipe-to-delete is the ask here.
                // Styling matches ReviewsListView's own List (see its
                // comment): .listRowBackground(.clear) +
                // .scrollContentBackground(.hidden) since a List row keeps
                // an opaque background of its own otherwise.
                List {
                    ForEach(viewModel.history) { entry in
                        NavigationLink {
                            ScheduleHistoryDetailView(historyId: entry.id, weekLabel: weekLabel(entry))
                        } label: {
                            row(entry)
                        }
                        .foregroundStyle(Color.cavnarInk)
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                        .listRowInsets(EdgeInsets(top: 6, leading: 20, bottom: 6, trailing: 20))
                        // Schedules never disappear on their own — this is
                        // the only removal path, deliberately requiring an
                        // explicit swipe, not a tap.
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button(role: .destructive) {
                                Haptic.light()
                                Task { await viewModel.delete(id: entry.id) }
                            } label: {
                                Label("Delete", systemImage: "trash")
                            }
                        }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        }
        .cavnarModuleBackground()
        .navigationTitle("Schedule History")
        .navigationBarTitleDisplayMode(.inline)
        .task { await viewModel.load() }
        .refreshable { await viewModel.load() }
        // A standard modal alert for a failed delete — surfaces the real
        // error without replacing the list underneath it (see
        // deleteErrorMessage's own comment on the view model).
        .alert("Couldn't delete schedule", isPresented: Binding(
            get: { viewModel.deleteErrorMessage != nil },
            set: { if !$0 { viewModel.deleteErrorMessage = nil } }
        )) {
            Button("OK", role: .cancel) { }
        } message: {
            Text(viewModel.deleteErrorMessage ?? "")
        }
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
                Text("\(scheduled.commaFormatted)h")
                    .font(.cavnarNumber(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk2)
            }
            // No manual chevron here — a NavigationLink inside a List
            // (unlike one in a plain ScrollView/VStack) already adds its
            // own system disclosure indicator for free, same as every
            // other List-based screen in this app (e.g. ReviewsListView).
            // This row used to add a second one on top of it.
        }
        // Distinct from the standard .cavnarCard() used everywhere else —
        // primarily orange fading into black (leading to trailing, since
        // these rows are wide and short, so a left-to-right fade actually
        // travels across the visible shape) with an orange outline,
        // deliberately called out from a plain neutral card. Text keeps
        // its normal ink colors — cavnarInk/Ink2/Ink3 are already proven
        // legible against ember-tinted grounds elsewhere in this app
        // (e.g. AskCavnarView's suggested-question pills). Was a flat
        // 2-stop fade (0.4 -> paper) that read as "mostly black with an
        // orange edge" — a 3-stop gradient holding strong orange through
        // the first half, THEN fading, actually reads as primarily orange
        // the way a 2-stop linear fade can't (a straight line from 0.4 to
        // 0 is already mostly faded by the midpoint).
        .padding(16)
        .background(
            LinearGradient(
                stops: [
                    .init(color: Color.cavnarEmber.opacity(0.75), location: 0),
                    .init(color: Color.cavnarEmber.opacity(0.55), location: 0.55),
                    .init(color: Color.cavnarPaper, location: 1),
                ],
                startPoint: .leading, endPoint: .trailing
            )
        )
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.cavnarEmber.opacity(0.55), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}
