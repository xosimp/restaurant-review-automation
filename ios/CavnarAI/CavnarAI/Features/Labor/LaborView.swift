import SwiftUI

private enum LaborSubTab: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case analytics = "Analytics"
    var id: String { rawValue }
}

struct LaborView: View {
    @State private var viewModel = LaborViewModel()
    @State private var analyticsViewModel = LaborAnalyticsViewModel()
    @State private var subTab: LaborSubTab = .overview

    var body: some View {
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: LaborSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 16)
                .padding(.top, 8)

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        if subTab == .overview {
                            if let stats = viewModel.stats {
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
                                // Same placement as the web Labor tab's Schedule
                                // panel — this used to live on the Analytics
                                // sub-tab, which put restaurant-level "why are my
                                // numbers what they are" advice one tap away from
                                // the actual schedule-generation workflow it's
                                // usually informing.
                                AIConsultantView(
                                    title: "Cavnar AI Labor Consultant",
                                    insight: analyticsViewModel.insight,
                                    isLoading: analyticsViewModel.isLoadingInsight
                                )
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
        .task { await viewModel.load() }
        .task { await analyticsViewModel.load() }
        .task { await viewModel.loadAvailability() }
    }

    @ViewBuilder
    private func heroCard(_ stats: LaborStats) -> some View {
        let tone: CavnarTone = stats.onTrack ? .good : .bad
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Labor cost", systemImage: "person.2.fill")
                    .font(.cavnarBody(11, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
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
            dataFreshnessNote(stats)

            ScheduleGenerateButton(
                tone: tone,
                isGenerating: viewModel.isGeneratingSchedule,
                action: { Task { await viewModel.generateSchedule() } }
            )
            .padding(.top, 2)
        }
        .cavnarGlassCard(tint: tone.foreground)
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

    /// Directly resolves the "why does this say Jun 1 in August" question —
    /// the week-bucketing math behind Overtime risk's date labels was
    /// already correct (real Monday-of-week from real shift dates); what
    /// was missing was ever telling the user WHICH week that data is
    /// actually from, so a real-but-months-old CSV upload (Gia Mia's actual
    /// case — a live shifts_csv covering Jun 1–14 with nothing uploaded
    /// since) silently looked like a stale/broken date instead of what it
    /// is: real historical data that's overdue for a refresh.
    @ViewBuilder
    private func dataFreshnessNote(_ stats: LaborStats) -> some View {
        if let start = stats.dateRange.start, let end = stats.dateRange.end,
           let startDate = Self.isoDayFormatter.date(from: start),
           let endDate = Self.isoDayFormatter.date(from: end) {
            let daysOld = Calendar.current.dateComponents([.day], from: endDate, to: Date()).day ?? 0
            let rangeText = "\(Self.displayDayFormatter.string(from: startDate)) – \(Self.displayDayFormatter.string(from: endDate))"
            let stale = stats.isLive && daysOld > 21
            HStack(alignment: .top, spacing: 5) {
                Image(systemName: stats.isLive ? (stale ? "exclamationmark.triangle.fill" : "clock") : "sparkles")
                    .font(.system(size: 10, weight: .semibold))
                Group {
                    if !stats.isLive {
                        Text("Showing sample data for illustration — upload your shifts CSV in Account for real numbers.")
                    } else if stale {
                        Text("Shift data is from \(rangeText) — \(daysOld) days old. Upload a fresher CSV for current numbers.")
                    } else {
                        Text("Based on shift data from \(rangeText).")
                    }
                }
            }
            .font(.cavnarBody(10, weight: 600))
            .foregroundStyle(stale ? Color.cavnarAmber : Color.cavnarInk3)
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
        CavnarDropdown(
            title: "Overtime risk",
            subtitle: "\(entries.count) \(entries.count == 1 ? "person" : "people") flagged this period",
            badge: atRisk > 0 ? atRisk : nil,
            tone: .bad,
            onExpand: { scrollToReveal(Self.overtimeID, proxy: proxy) }
        ) {
            VStack(spacing: 8) {
                ForEach(entries) { entry in
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
        VStack(alignment: .leading, spacing: 10) {
            Text("By role")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            RoleDonutChart(roles: roles)
                .cavnarCard()
        }
    }

    @ViewBuilder
    private func overstaffedDropdown(_ days: [LaborOverstaffedDay], proxy: ScrollViewProxy) -> some View {
        CavnarDropdown(
            title: "Overstaffed days", subtitle: "where the money is going",
            badge: days.count, tone: .bad,
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

    @ViewBuilder
    private func scheduleResultSection(_ result: GeneratedSchedule) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                if let hours = result.hoursScheduled {
                    (Text(String(format: "%.1f", hours)).font(.cavnarNumber(13, weight: 600)) + Text(" hours scheduled"))
                        .font(.cavnarBody(13, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                }
                Spacer()
                if let csv = result.scheduleCsv {
                    ShareLink(item: csv, preview: SharePreview("Schedule.csv")) {
                        Image(systemName: "square.and.arrow.up")
                    }
                }
            }
            ForEach(result.summary ?? [], id: \.self) { line in
                Text("• \(line)")
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk2)
            }
            if let budget = result.hoursBudget, budget > 0, let scheduled = result.hoursScheduled {
                parHoursBanner(budget: budget, scheduled: scheduled, dollars: result.laborBudgetDollars)
            }
        }
        .cavnarCard()

        if let rows = result.previewRows, !rows.isEmpty {
            fullScheduleTable(rows)
        }
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

    @ViewBuilder
    private func fullScheduleTable(_ rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Full schedule")
                .font(.cavnarBody(12, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            VStack(spacing: 6) {
                ForEach(rows) { row in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(row.employee ?? "").font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text("\(row.day ?? "") · \(row.role ?? "")").font(.cavnarBody(10)).foregroundStyle(Color.cavnarInk3)
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
                        ProgressView().tint(tone.foreground)
                    } else {
                        Image(systemName: "sparkles")
                            .font(.system(size: 15, weight: .bold))
                            .foregroundStyle(tone.foreground)
                    }
                }
                .frame(width: 34, height: 34)

                VStack(alignment: .leading, spacing: 1) {
                    Text(isGenerating ? "Generating your schedule…" : "Generate next week's schedule")
                        .font(.cavnarBody(15, weight: 700))
                        .foregroundStyle(tone.foreground)
                    if !isGenerating {
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
            .background(Color.cavnarPaper)
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
