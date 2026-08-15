import SwiftUI

private enum LaborSubTab: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case analytics = "Analytics"
    var id: String { rawValue }
}

struct LaborView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var viewModel = LaborViewModel()
    @State private var analyticsViewModel = LaborAnalyticsViewModel()
    @State private var subTab: LaborSubTab = .overview
    @State private var showDataInfo = false

    var body: some View {
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: LaborSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 20)
                .padding(.top, 8)
                .padding(.bottom, 16)

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        if subTab == .overview {
                            if let stats = viewModel.stats {
                                // Above the hero banner — reads as Cavnar's
                                // own opening take on the week before the
                                // budget/schedule numbers, not a footnote
                                // wedged between other cards.
                                AIConsultantView(
                                    title: "Cavnar AI Labor Consultant",
                                    insight: analyticsViewModel.insight,
                                    isLoading: analyticsViewModel.isLoadingInsight
                                )
                                heroCard(stats)
                                if !stats.laborUpcoming.isEmpty {
                                    upcomingEventsCard(stats.laborUpcoming)
                                }
                                if let result = viewModel.scheduleResult, result.ok {
                                    scheduleResultSection(result)
                                }
                                if let error = viewModel.scheduleError {
                                    Text(error)
                                        .font(.cavnarBody(12))
                                        .foregroundStyle(Color.cavnarRed)
                                }
                                // By role sits directly above the other three
                                // dropdowns (Overtime/Overstaffed/Understaffed)
                                // so all four read as one connected group —
                                // Overtime used to sit above the donut chart,
                                // visually separating it from the rest.
                                if !stats.roleSummary.isEmpty {
                                    roleSection(stats.roleSummary)
                                }
                                if !stats.overtimeRisk.isEmpty {
                                    overtimeDropdown(stats.overtimeRisk, proxy: proxy)
                                }
                                if !stats.overstaffedDays.isEmpty {
                                    overstaffedDropdown(stats.overstaffedDays, proxy: proxy)
                                }
                                if !stats.understaffedDays.isEmpty {
                                    understaffedDropdown(stats.understaffedDays, proxy: proxy)
                                }
                                AvailabilityManagerSection(viewModel: viewModel) {
                                    scrollToReveal(Self.availabilityID, proxy: proxy)
                                }
                                .id(Self.availabilityID)
                            } else if viewModel.isLoading {
                                ProgressView().padding(.top, 60)
                            } else if let error = viewModel.errorMessage {
                                VStack(spacing: 8) {
                                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                                    Button("Retry") { Task { await viewModel.load() } }
                                }
                                .padding(.top, 60)
                                .frame(maxWidth: .infinity)
                            }
                        } else {
                            LaborAnalyticsSection(viewModel: analyticsViewModel, laborStats: viewModel.stats)
                        }
                    }
                    .padding(20)
                }
            }
        }
        .cavnarModuleBackground()
        .refreshable {
            await viewModel.load()
            await viewModel.loadAvailability()
        }
        .navigationTitle("Labor")
        .navigationBarTitleDisplayMode(.inline)
        .cavnarEmberTitle("Labor")
        .cavnarEmberBackButton()
        .task {
            // Loaded synchronously from disk before either network call —
            // shows the last known schedule/insight immediately on a fresh
            // launch instead of an empty tab while the real fetch is still
            // in flight, and stops a relaunch from silently discarding a
            // schedule that was only ever held in memory.
            if let restaurantId = sessionStore.currentUser?.restaurantId {
                viewModel.configureCaching(restaurantId: restaurantId)
                analyticsViewModel.configureCaching(restaurantId: restaurantId)
            }
            await viewModel.load()
        }
        .task { await analyticsViewModel.load() }
        .task { await viewModel.loadAvailability() }
        // Belt-and-suspenders alongside the .task-time restore above: tied
        // directly to scenePhase (the same signal RootView's own Face ID
        // lock keys off — confirmed swiping away and immediately back
        // still round-trips through .background) rather than depending on
        // whether SwiftUI actually re-runs .task for this specific
        // TabView/if-else hierarchy on a quick foreground return, which a
        // reported-still-missing schedule after the .task fix suggests
        // isn't reliably happening the way the .task fix assumed. This
        // costs nothing when scheduleResult is already populated —
        // configureCaching only ever sets it from a valid cache entry, it
        // never clears an existing value.
        .onChange(of: scenePhase) { _, newPhase in
            guard newPhase == .active, let restaurantId = sessionStore.currentUser?.restaurantId else { return }
            viewModel.configureCaching(restaurantId: restaurantId)
        }
    }

    @ViewBuilder
    private func heroCard(_ stats: LaborStats) -> some View {
        let tone: CavnarTone = stats.onTrack ? .good : .bad
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Label("Labor cost", systemImage: "person.2.fill")
                    .font(.cavnarBody(11, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                dataFreshnessInfoButton(stats)
                Spacer()
                TonePill(text: stats.onTrack ? "On track" : "Over target", tone: tone)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(String(format: "%.1f%%", stats.overallLaborPct))
                    .font(.cavnarNumber(32, weight: 500))
                    .foregroundStyle(Color.cavnarInk)
                    .cavnarNumberGlow()
                (Text("/ ") + Text("\(Int(stats.target))%").font(.cavnarNumber(14, weight: 600)) + Text(" target"))
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
            }
            StatProgressBar(progress: stats.overallLaborPct / max(stats.target, 1), tone: tone)
            if stats.potentialSavings > 0 {
                (Text("Est. ") + Text("$\(Int(stats.potentialSavings))").font(.cavnarNumber(12, weight: 600)) + Text(" in optimized-scheduling savings available"))
                    .font(.cavnarBody(12, weight: 600))
                    .foregroundStyle(Color.cavnarAmber)
            }

            ScheduleGenerateButton(
                tone: tone,
                isGenerating: viewModel.isGeneratingSchedule,
                action: { Task { await viewModel.generateSchedule() } }
            )
            .padding(.top, 2)
        }
        .cavnarGlassCard(tint: tone.foreground)
    }

    private struct DataFreshness {
        let rangeText: String
        let daysOld: Int
        let stale: Bool
        let isLive: Bool
    }

    private func freshnessInfo(_ stats: LaborStats) -> DataFreshness? {
        guard let start = stats.dateRange.start, let end = stats.dateRange.end,
              let startDate = Self.isoDayFormatter.date(from: start),
              let endDate = Self.isoDayFormatter.date(from: end) else { return nil }
        let daysOld = Calendar.current.dateComponents([.day], from: endDate, to: Date()).day ?? 0
        let rangeText = "\(Self.displayDayFormatter.string(from: startDate)) – \(Self.displayDayFormatter.string(from: endDate))"
        return DataFreshness(rangeText: rangeText, daysOld: daysOld, stale: stats.isLive && daysOld > 21, isLive: stats.isLive)
    }

    /// Was an always-visible amber text row under the hero numbers — moved
    /// behind a tap so the card's headline stat isn't sharing the spotlight
    /// with a line about data provenance every time you glance at it. The
    /// icon itself still tells you at a glance whether there's something to
    /// check (amber + triangle when the underlying shift data is stale),
    /// without spelling it out until asked.
    @ViewBuilder
    private func dataFreshnessInfoButton(_ stats: LaborStats) -> some View {
        if let info = freshnessInfo(stats) {
            Button {
                Haptic.light()
                showDataInfo = true
            } label: {
                Image(systemName: info.stale ? "exclamationmark.circle.fill" : "info.circle")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(info.stale ? Color.cavnarAmber : Color.cavnarInk3)
            }
            .buttonStyle(.plain)
            .popover(isPresented: $showDataInfo, arrowEdge: .bottom) {
                dataFreshnessPopoverContent(info)
                    .presentationCompactAdaptation(.popover)
                    // The content's own .background() doesn't get clipped to
                    // the popover's actual (rounded) presentation shape, so
                    // its square corners peeked out past — or fell short
                    // of — the system's rounded chrome, reading as a
                    // mismatched border. presentationBackground draws at the
                    // right layer, clipped to the real shape.
                    .presentationBackground(Color.cavnarPaper2)
            }
        }
    }

    private func dataFreshnessPopoverContent(_ info: DataFreshness) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(info.isLive ? "Shift data window" : "Sample data")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarEmber)
            Group {
                if !info.isLive {
                    Text("Showing sample data for illustration — upload your shifts CSV in Account for real numbers.")
                } else if info.stale {
                    Text("Shift data is from \(info.rangeText) — \(info.daysOld) days old. Upload a fresher CSV for current numbers.")
                } else {
                    Text("Based on shift data from \(info.rangeText).")
                }
            }
            .font(.cavnarBody(12))
            .foregroundStyle(Color.cavnarInk2)
            // Without this the popover sized itself to the text's
            // unconstrained ideal (single-line) width first and only then
            // applied the frame below, clipping everything past ~7-8 words
            // instead of wrapping. This forces wrap-not-clip within
            // whatever width it's actually given.
            .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        // A fixed width (not maxWidth) gives the popover's own auto-sizing
        // an unambiguous number to lay out against, rather than an upper
        // bound it could compute around inconsistently.
        .frame(width: 240, alignment: .leading)
    }

    private static let availabilityID = "labor-availability"

    /// Scrolls the just-opened section into view once its expand animation
    /// has room to settle — firing scrollTo in the same instant as the
    /// disclosure's own height-change animation reliably centers against
    /// the pre-expansion layout, not the taller one about to exist a beat
    /// later. A short delay matched to CavnarDropdown's 0.22s expand
    /// animation lets the new height land first.
    private func scrollToReveal(_ id: String, proxy: ScrollViewProxy) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.24) {
            withAnimation(.easeOut(duration: 0.25)) {
                proxy.scrollTo(id, anchor: .center)
            }
        }
    }

    private static let isoDayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "UTC")
        return f
    }()

    private static let displayDayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMM d"
        return f
    }()

    @ViewBuilder
    private func upcomingEventsCard(_ events: [LaborUpcomingEvent]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "calendar")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber)
                Text("Scheduling forecast")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(0.8)
                    .foregroundStyle(Color.cavnarEmber)
            }
            VStack(alignment: .leading, spacing: 10) {
                ForEach(events) { event in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(event.name)
                                .font(.cavnarBody(13, weight: 700))
                                .foregroundStyle(Color.cavnarInk)
                            Text(daysAwayLabel(event.daysAway))
                                .font(.cavnarBody(11))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        Text(forecastCopy(daysAway: event.daysAway))
                            .font(.cavnarBody(11))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                }
            }
        }
        .cavnarCard()
    }

    private func daysAwayLabel(_ days: Int) -> String {
        if days == 0 { return "today" }
        if days == 1 { return "tomorrow" }
        return "\(days) days away"
    }

    private func forecastCopy(daysAway: Int) -> String {
        if daysAway <= 3 {
            return "Expect elevated covers — confirm full kitchen and floor coverage."
        } else if daysAway <= 7 {
            return "Check this week's schedule now — add 1–2 staff if you're typically at capacity."
        } else {
            return "Flag for your next schedule build."
        }
    }

    private static let overtimeID = "labor-overtime"
    private static let overstaffedID = "labor-overstaffed"
    private static let understaffedID = "labor-understaffed"

    @ViewBuilder
    private func overtimeDropdown(_ entries: [LaborOvertimeEntry], proxy: ScrollViewProxy) -> some View {
        let atRisk = entries.filter { $0.status == "overtime" && !($0.otAllowed ?? false) }.count
        // Backend returns these in whatever order Python's dict iteration
        // happened to produce (first-seen-in-the-CSV order), which reads as
        // random — sorted lowest-to-highest hours here instead.
        let sorted = entries.sorted { ($0.hours ?? 0) < ($1.hours ?? 0) }
        CavnarDropdown(
            title: "Overtime risk",
            subtitle: "\(entries.count) \(entries.count == 1 ? "person" : "people") flagged this period",
            badge: atRisk > 0 ? atRisk : nil,
            tone: .bad,
            isExpanded: $viewModel.overtimeExpanded,
            onExpand: { scrollToReveal(Self.overtimeID, proxy: proxy) }
        ) {
            VStack(spacing: 8) {
                ForEach(sorted) { entry in
                    overtimeRow(entry)
                }
            }
        }
        .id(Self.overtimeID)
    }

    private func overtimeRow(_ entry: LaborOvertimeEntry) -> some View {
        let allowed = entry.otAllowed ?? false
        let isOvertime = entry.status == "overtime"
        let rowTone: Color = allowed ? Color.cavnarGreen : (isOvertime ? Color.cavnarRed : Color.cavnarAmber)
        return HStack {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(entry.employee ?? "Unknown")
                        .font(.cavnarBody(13, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    if allowed {
                        Text("CONSTRAINT")
                            .font(.cavnarBody(8, weight: 700))
                            .tracking(0.4)
                            .foregroundStyle(Color.cavnarGreen)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.cavnarGreenBg)
                            .clipShape(Capsule())
                    }
                }
                HStack(spacing: 4) {
                    Text("Week of \(entry.week ?? "—")")
                    if let total = entry.totalHours {
                        Text("· \(String(format: "%.1f", total))h total")
                    }
                }
                .font(.cavnarBody(11))
                .foregroundStyle(Color.cavnarInk3)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text("\(entry.hours.map { String(format: "%.1f", $0) } ?? "—")h")
                    .font(.cavnarNumber(14, weight: 600))
                    .foregroundStyle(rowTone)
                Text(allowed ? "OT allowed" : (isOvertime ? "Review pay" : "Approaching 40h"))
                    .font(.cavnarBody(9, weight: 700))
                    .foregroundStyle(rowTone)
            }
        }
        .padding(10)
        .background(rowTone.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    @ViewBuilder
    private func roleSection(_ roles: [LaborRoleSummary]) -> some View {
        // Deliberately not wrapped in .cavnarCard() — every other section on
        // this tab is a bordered box, and stacking one more made the page
        // read as an unbroken column of boxes. Let the chart float directly
        // on the page background instead.
        VStack(alignment: .leading, spacing: 14) {
            Text("By role")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            RoleDonutChart(roles: roles)
        }
    }

    @ViewBuilder
    private func overstaffedDropdown(_ days: [LaborOverstaffedDay], proxy: ScrollViewProxy) -> some View {
        CavnarDropdown(
            title: "Overstaffed days", subtitle: "where the money is going",
            badge: days.count, tone: .bad,
            isExpanded: $viewModel.overstaffedExpanded,
            onExpand: { scrollToReveal(Self.overstaffedID, proxy: proxy) }
        ) {
            VStack(spacing: 8) {
                ForEach(Array(days.prefix(10))) { day in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(day.day).font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text(day.date).font(.cavnarBody(10)).foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 1) {
                            Text("$\(Int(day.sales))").font(.cavnarNumber(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text("\(String(format: "%.1f", day.laborPct))% labor").font(.cavnarBody(10, weight: 600)).foregroundStyle(Color.cavnarRed)
                        }
                    }
                    .padding(10)
                    .background(Color.cavnarRed.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                }
                if days.count > 10 {
                    Text("+ \(days.count - 10) more").font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                }
            }
        }
        .id(Self.overstaffedID)
    }

    @ViewBuilder
    private func understaffedDropdown(_ days: [LaborUnderstaffedDay], proxy: ScrollViewProxy) -> some View {
        CavnarDropdown(
            title: "Understaffed days", subtitle: "possible missed revenue",
            badge: days.count, tone: .warning,
            isExpanded: $viewModel.understaffedExpanded,
            onExpand: { scrollToReveal(Self.understaffedID, proxy: proxy) }
        ) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(days.prefix(10))) { day in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(day.day).font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text(day.date).font(.cavnarBody(10)).foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 1) {
                            Text("$\(Int(day.sales))").font(.cavnarNumber(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text("\(String(format: "%.1f", day.laborPct))% labor").font(.cavnarBody(10, weight: 600)).foregroundStyle(Color.cavnarAmber)
                        }
                    }
                    .padding(10)
                    .background(Color.cavnarAmber.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                }
                if days.count > 10 {
                    Text("+ \(days.count - 10) more").font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                }
                Text("💡 Consider adding 1–2 staff on \(days.prefix(3).map(\.day).joined(separator: ", "))\(days.count > 3 ? " and more" : ".")")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.top, 2)
            }
        }
        .id(Self.understaffedID)
    }

    /// Wrapped in a dropdown (starting open on a freshly-generated result,
    /// but otherwise following whatever the user last left it at — see
    /// LaborViewModel.scheduleResultExpanded) instead of always-rendered —
    /// a full schedule with its summary and day-by-day table has no way to
    /// be hidden afterward otherwise, and stays pinned at that height for
    /// the rest of the session.
    @ViewBuilder
    private func scheduleResultSection(_ result: GeneratedSchedule) -> some View {
        CavnarDropdown(
            title: "Generated schedule",
            subtitle: scheduleSubtitle(result),
            tone: .good,
            isExpanded: $viewModel.scheduleResultExpanded
        ) {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 12) {
                    if let summary = result.summary, !summary.isEmpty {
                        // Same heading + color the web schedule-preview panel
                        // uses for this exact block, so a client without
                        // context for a bare bullet list knows what it's
                        // looking at: the AI's own reasoning for this
                        // specific week's shifts, not generic tips.
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
                    if let budget = result.hoursBudget, budget > 0, let scheduled = result.hoursScheduled {
                        parHoursBanner(budget: budget, scheduled: scheduled, dollars: result.laborBudgetDollars)
                    }
                }
                .cavnarCard()

                if let rows = result.previewRows, !rows.isEmpty {
                    fullScheduleTable(rows, csv: result.scheduleCsv)
                }
            }
        }
    }

    /// Date range first, so a client sees at a glance which week this is
    /// for, then hours scheduled — reuses the same ISO/display formatters
    /// the freshness popover already parses shift-data dates with.
    private func scheduleSubtitle(_ result: GeneratedSchedule) -> String? {
        var parts: [String] = []
        if let first = result.weekDates?.first, let last = result.weekDates?.last,
           let startDate = Self.isoDayFormatter.date(from: first),
           let endDate = Self.isoDayFormatter.date(from: last) {
            parts.append("\(Self.displayDayFormatter.string(from: startDate)) – \(Self.displayDayFormatter.string(from: endDate))")
        }
        if let hours = result.hoursScheduled {
            parts.append("\(String(format: "%.1f", hours))h scheduled")
        }
        return parts.isEmpty ? nil : parts.joined(separator: "  ·  ")
    }

    private func parHoursBanner(budget: Double, scheduled: Double, dollars: Double?) -> some View {
        let diff = scheduled - budget
        let withinRange = abs(diff) <= max(budget * 0.05, 1)
        return HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text("PAR HOURS CHECK")
                    .font(.cavnarBody(9, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarGreen)
                Text("Budgeted \(String(format: "%.0f", budget))h\(dollars.map { " ($\(Int($0)))" } ?? "") for the week")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk2)
            }
            Spacer()
            Text(withinRange ? "On budget" : (diff > 0 ? "+\(String(format: "%.0f", diff))h over" : "\(String(format: "%.0f", diff))h under"))
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(withinRange ? Color.cavnarGreen : Color.cavnarAmber)
        }
        .padding(10)
        .background(Color.cavnarGreen.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private static let scheduleDayOrder = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    /// Grouped by day with one branded header per day instead of repeating
    /// the day name under every single row — a client scanning this can
    /// find "Monday" once and read straight down its staff, rather than
    /// re-reading the same day label six times in a row.
    ///
    /// The backend re-derives `day` from the row's `date` server-side, so
    /// every row's day value is a real weekday now even when the AI's raw
    /// CSV had that column scrambled — `day` alone can no longer signal a
    /// bad row. `needsReview` is the explicit flag the backend sets when a
    /// row's other columns (employee/role/times) were scrambled badly
    /// enough that it couldn't confidently auto-repair them; those still
    /// get routed to a separate flagged group instead of rendering as a
    /// normal (but silently wrong) day entry.
    @ViewBuilder
    private func fullScheduleTable(_ rows: [ScheduleRow], csv: String?) -> some View {
        let recognized = rows.filter { $0.needsReview != true }
        let unrecognized = rows.filter { $0.needsReview == true }
        let grouped = Dictionary(grouping: recognized, by: { $0.day ?? "—" })
        let orderedDays = Self.scheduleDayOrder.filter { grouped[$0] != nil }

        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Full schedule")
                    .font(.cavnarBody(12, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer()
                // Moved here from the summary card above — sitting next to
                // the table it actually exports reads far more directly
                // than floating next to an unrelated "hours scheduled" line.
                if let csv {
                    ShareLink(item: csv, preview: SharePreview("Schedule.csv")) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                }
            }
            ForEach(orderedDays, id: \.self) { day in
                scheduleDayGroup(day: day, rows: grouped[day] ?? [])
            }
            if !unrecognized.isEmpty {
                needsReviewGroup(unrecognized)
            }
        }
    }

    private func needsReviewGroup(_ rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.cavnarAmber)
                Text("NEEDS REVIEW")
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarAmber)
            }
            Text("These rows didn't come back with a normal weekday — double-check them before publishing.")
                .font(.cavnarBody(10))
                .foregroundStyle(Color.cavnarInk3)
            VStack(spacing: 6) {
                ForEach(rows) { row in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(row.employee ?? row.day ?? "Unknown")
                                .font(.cavnarBody(12, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Text("day: \(row.day ?? "—")  ·  role: \(row.role ?? "—")")
                                .font(.cavnarBody(10))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer()
                        Text("\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")")
                            .font(.cavnarNumber(11))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                    .padding(.vertical, 4)
                }
            }
            .cavnarCard()
        }
        .padding(10)
        .background(Color.cavnarAmber.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private func scheduleDayGroup(day: String, rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text(day.uppercased())
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarEmber)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Color.cavnarEmber.opacity(0.16))
                    .clipShape(Capsule())
                Text("\(rows.count) shift\(rows.count == 1 ? "" : "s")")
                    .font(.cavnarBody(10))
                    .foregroundStyle(Color.cavnarInk3)
            }
            VStack(spacing: 6) {
                ForEach(rows) { row in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(row.employee ?? "").font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                            if let role = row.role, !role.isEmpty {
                                Text(role).font(.cavnarBody(10)).foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        Spacer()
                        Text("\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")")
                            .font(.cavnarNumber(11))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                    .padding(.vertical, 4)
                }
            }
            .cavnarCard()
        }
    }
}

/// The AI schedule generator is Gia Mia's single most-used feature —
/// deliberately styled as the standout action on the page (gradient fill,
/// icon badge, glow shadow, subtitle) rather than a plain text button, and
/// placed inside the hero card near the top instead of at the bottom of a
/// long scroll.
/// Matches the hero card's own status language instead of introducing a
/// third color (orange, clashing against a card that's already tinted red
/// or green depending on whether labor is over/under target) — a dark
/// surface with the same green/red used by the "On track"/"Over target"
/// pill above it, so the button reads as an extension of that card's own
/// status rather than an unrelated CTA dropped on top of it.
private struct ScheduleGenerateButton: View {
    let tone: CavnarTone
    let isGenerating: Bool
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: 12) {
                ZStack {
                    Circle().fill(tone.foreground.opacity(0.16))
                    if isGenerating {
                        PulsingSparkleIcon(color: tone.foreground)
                    } else {
                        Image(systemName: "sparkles")
                            .font(.system(size: 15, weight: .bold))
                            .foregroundStyle(tone.foreground)
                    }
                }
                .frame(width: 34, height: 34)

                if isGenerating {
                    ScheduleLoadingText(color: tone.foreground)
                } else {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Generate next week's schedule")
                            .font(.cavnarBody(15, weight: 700))
                            .foregroundStyle(tone.foreground)
                        Text("AI-optimized from your sales & shift history")
                            .font(.cavnarBody(10, weight: 500))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }

                Spacer(minLength: 4)

                if !isGenerating {
                    Image(systemName: "arrow.right")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(tone.foreground.opacity(0.85))
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            // Same background token TonePill uses for its "On track"/"Over
            // target" pill (a low-opacity tint of the tone color) — so the
            // button reads as the exact same surface, not a separately
            // chosen dark that happens to be similar.
            .background(tone.background)
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(tone.foreground.opacity(0.4), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .shadow(color: tone.foreground.opacity(0.3), radius: 14, x: 0, y: 6)
        }
        .buttonStyle(.plain)
        .disabled(isGenerating)
        .scaleEffect(isGenerating ? 0.99 : 1)
        .animation(.easeOut(duration: 0.15), value: isGenerating)
        .sensoryFeedback(.impact(weight: .medium), trigger: isGenerating) { old, new in !old && new }
    }
}

/// Gentle breathing opacity/scale pulse instead of a spinning ProgressView
/// — reused only while the button is in its generating state (a fresh
/// instance is created each time isGenerating flips true, so the pulse
/// restarts cleanly rather than needing manual state resets).
private struct PulsingSparkleIcon: View {
    let color: Color
    @State private var pulse = false

    var body: some View {
        Image(systemName: "sparkles")
            .font(.system(size: 15, weight: .bold))
            .foregroundStyle(color)
            .opacity(pulse ? 1 : 0.45)
            .scaleEffect(pulse ? 1.08 : 0.9)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) {
                    pulse = true
                }
            }
    }
}

/// Cycles through a handful of status lines while the schedule generates,
/// cross-fading between them — replaces a single static "Generating…"
/// label (and the bare spinner it sat next to) with something that reads
/// as the AI actually working through real steps, matching how long a
/// real Claude call over a week of shift/sales/weather/YoY context
/// actually takes rather than an indeterminate wait.
private struct ScheduleLoadingText: View {
    let color: Color

    // "Usually takes..." was 25–35s, set before this was measured against
    // a real server log — an actual generation (full shift history + YoY +
    // weather + the PAR-reconciliation prompt) took ~71s end to end.
    // Rounded up rather than quoting a precise range that varies run to
    // run.
    private static let messages = [
        "Reviewing your sales & shift history…",
        "Usually takes about a minute…",
        "Balancing coverage across the week…",
        "Checking for overtime risk…",
        "Weighing upcoming events & weather…",
        "Reconciling against your labor budget…",
        "Almost there…",
    ]

    @State private var index = 0

    var body: some View {
        ShimmerText(text: Self.messages[index], font: .cavnarBody(13, weight: 700), color: color)
            .task {
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(3.5))
                    if Task.isCancelled { return }
                    withAnimation(.easeInOut(duration: 0.4)) {
                        index = (index + 1) % Self.messages.count
                    }
                }
            }
    }
}

/// A bright band sweeping left-to-right across the text, masked to its own
/// glyph shape — the same "reading a file" shimmer pattern common in AI
/// tools, reusing the sliding-gradient technique CavnarSkeletonBar already
/// establishes for loading states elsewhere in the app, just masked to
/// text instead of filling a bar.
///
/// Driven by TimelineView (real wall-clock time) rather than a toggled
/// @State + withAnimation(.repeatForever) — the first version used the
/// latter and the sweep only ever ran on the very first message: the
/// parent's own withAnimation(...) { index += 1 } for the cross-fade
/// between messages is an ambient transaction that gets applied to
/// whatever animatable properties re-render inside it, and it silently
/// overrode/replaced the repeat-forever animation on this view's offset
/// with that one-shot transition every time the text changed. Computing
/// the sweep position directly from timeline.date sidesteps SwiftUI's
/// animation/transaction system entirely, so no ambient animation from a
/// parent update can interrupt it.
private struct ShimmerText: View {
    let text: String
    let font: Font
    let color: Color

    private static let period: Double = 1.6

    var body: some View {
        TimelineView(.animation) { timeline in
            let elapsed = timeline.date.timeIntervalSinceReferenceDate
            let phase = (elapsed.truncatingRemainder(dividingBy: Self.period)) / Self.period

            Text(text)
                .font(font)
                .foregroundStyle(color.opacity(0.4))
                .contentTransition(.opacity)
                .overlay(
                    GeometryReader { geo in
                        LinearGradient(
                            colors: [.clear, color, .clear],
                            startPoint: .leading, endPoint: .trailing
                        )
                        .frame(width: geo.size.width * 0.6)
                        .offset(x: -geo.size.width * 0.6 + phase * geo.size.width * 1.6)
                    }
                    .mask(Text(text).font(font))
                    .allowsHitTesting(false)
                )
        }
    }
}
