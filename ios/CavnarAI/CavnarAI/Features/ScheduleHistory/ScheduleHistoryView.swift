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

    @State private var clock = CavnarEntranceClock()

    // Own NavigationStack, not pushed onto a parent's — presented as a
    // sheet from AccountView now (see its own comment), matching how
    // every other modal in this app opens, sliding up from the bottom.
    // ScheduleHistoryDetailView still pushes normally *within* this
    // stack when a row is tapped — only the outermost presentation
    // changed, drill-down navigation inside the sheet is unaffected.
    var body: some View {
        NavigationStack {
        Group {
            if viewModel.history.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                CavnarEmptyHearth(
                    title: "No schedules generated yet",
                    message: "Every schedule you generate in Labor is kept here."
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .padding(.top, 40)
            } else if viewModel.isLoading && viewModel.history.isEmpty {
                // See ChangelogView/AccountView/ModulesGridView's identical
                // fix — a bare loading view with no frame let
                // .cavnarModuleBackground()'s wash flash as a narrow
                // rectangle instead of full-screen.
                CavnarLoadingSeal()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
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
                    ForEach(Array(viewModel.history.enumerated()), id: \.element.id) { index, entry in
                        NavigationLink {
                            ScheduleHistoryDetailView(historyId: entry.id, weekLabel: weekLabel(entry))
                        } label: {
                            row(entry)
                        }
                        .foregroundStyle(Color.cavnarInk)
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                        .listRowInsets(EdgeInsets(top: 6, leading: 20, bottom: 6, trailing: 20))
                        .cavnarRowEntrance(index: index, clock: clock)
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
        .toolbar { cavnarTitleToolbar("Schedule History") }
        .task { await viewModel.load() }
        .cavnarEmberRefreshable { await viewModel.load() }
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
                        .font(.cavnarBody(14))
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
        // (e.g. AskCavnarView's suggested-question pills).
        //
        // The orange side of earlier versions was tuned right, but the
        // black side kept reading as a hard cutoff no matter how many
        // stops led into it — because the LAST stop was a fully *opaque*
        // solid Color.cavnarPaper, jumping straight from a translucent
        // ember (alpha ~0.15) to alpha 1.0 of a different hue in one
        // step. That alpha cliff is what "jarring" was — not the stop
        // count. Fix: paint solid cavnarPaper as a base layer, then let
        // the ember gradient itself fade to fully *transparent* alpha 0,
        // never switching color. With alpha decreasing continuously to
        // zero over an already-cavnarPaper-colored base, there's no seam
        // left to be jarring.
        .padding(15)
        .background(
            ZStack {
                Color.cavnarPaper
                CavnarEmberFade.horizontal
            }
        )
        // Inset (not a plain strokeBorder at the true edge) so this reads
        // as an inner highlight line sitting just inside the pill's
        // boundary, not a frame drawn around the outside of it — and
        // painted with the exact same gradient as the fill above (not a
        // flat orange) so the border and the background it's tracing
        // fade together as one continuous surface, instead of a
        // constant-strength orange ring sitting on top of a fill that's
        // fading out underneath it.
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .inset(by: 1)
                .strokeBorder(CavnarEmberFade.horizontal, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        // Dark shadow biased downward so it reads as peeking out from
        // under the card rather than a diffuse glow all the way around —
        // y-offset roughly matches the blur radius so most of the shadow
        // falls below the pill instead of evenly on every side. Matches
        // the Tracker tab's IngredientCard exactly (FoodCostQuickEntryView
        // .swift) — was 0.45/10/6 here, a small unintentional drift from
        // that card's 0.4/8/5, now brought back in line.
        .shadow(color: .black.opacity(0.4), radius: 8, x: 0, y: 5)
    }
}
