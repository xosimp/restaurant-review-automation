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
                // maxWidth/maxHeight matter here, not just centering —
                // .cavnarModuleBackground()'s ember wash sizes to this
                // Group, and a bare ProgressView hugging its own tiny
                // intrinsic size made the wash flash as a narrow
                // rectangle instead of full-screen for the split second
                // this state is visible (this was the "weird shape on
                // first load" bug — also present, and also fixed, on
                // AccountView/ModulesGridView's identical loading states).
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error = viewModel.errorMessage {
                VStack(spacing: 10) {
                    Text("Couldn't load this schedule").font(.cavnarBody(15, weight: 700))
                    Text(error).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk2).multilineTextAlignment(.center)
                    Button("Retry") { Task { await viewModel.load(id: historyId) } }
                        .buttonStyle(CavnarPrimaryButtonStyle())
                        .padding(.top, 4)
                }
                .padding(24)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
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
            //
            // The ToolbarItem itself is unconditional now — it used to
            // only exist once scheduleCsv was available, which meant the
            // icon visibly popped into an empty toolbar slot a moment
            // after the screen appeared. Keeping the slot always present
            // and just swapping a dimmed, inert icon in for the real
            // ShareLink until there's something to share removes that
            // pop-in; nothing about the toolbar's layout changes once
            // loading finishes; only the icon's opacity and function do.
            ToolbarItem(placement: .topBarTrailing) {
                if let csv = viewModel.detail?.scheduleCsv {
                    ShareLink(item: csv, preview: SharePreview("Schedule — \(weekLabel).csv")) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                } else {
                    Image(systemName: "square.and.arrow.up")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber.opacity(0.3))
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

    // Same fixed team order and per-role clustering as LaborView's own
    // fullScheduleTable (see its groupedByRole/roleCategory doc comment
    // for the full reasoning) — a past schedule read from history should
    // look exactly as organized as a freshly generated one, not revert to
    // whatever raw order the AI originally wrote the CSV in.
    private static let roleCategoryOrder = ["cook", "host", "busser", "runner", "bartender", "server", "supervisor"]

    private static func roleCategory(_ role: String?) -> String {
        let r = (role ?? "").lowercased()
        if r.contains("cook") || r.contains("kitchen") { return "cook" }
        if r.contains("host") || r.contains("carry") { return "host" }
        if r.contains("buss") { return "busser" }
        if r.contains("runner") { return "runner" }
        if r.contains("bartend") { return "bartender" }
        if r.contains("supervisor") || r.contains("shift lead") { return "supervisor" }
        if r.contains("server") { return "server" }
        return "other"
    }

    private static func groupedByRole(_ rows: [ScheduleRow]) -> [ScheduleRow] {
        var categoriesSeen: [String] = []
        var rolesByCategory: [String: [String]] = [:]
        var rowsByExactRole: [String: [ScheduleRow]] = [:]
        for row in rows {
            let category = roleCategory(row.role)
            let exactRole = row.role ?? ""
            if !categoriesSeen.contains(category) { categoriesSeen.append(category) }
            if rowsByExactRole[exactRole] == nil {
                rowsByExactRole[exactRole] = []
                rolesByCategory[category, default: []].append(exactRole)
            }
            rowsByExactRole[exactRole]?.append(row)
        }
        let orderedCategories = roleCategoryOrder.filter(categoriesSeen.contains)
            + categoriesSeen.filter { !roleCategoryOrder.contains($0) }
        return orderedCategories.flatMap { category in
            (rolesByCategory[category] ?? []).flatMap { rowsByExactRole[$0] ?? [] }
        }
    }

    @ViewBuilder
    private func scheduleByDay(_ rows: [ScheduleRow]) -> some View {
        let grouped = Dictionary(grouping: rows) { $0.day ?? "" }
        VStack(alignment: .leading, spacing: 16) {
            ForEach(Self.dayOrder.filter { grouped[$0] != nil }, id: \.self) { day in
                scheduleDayGroup(day: day, rows: grouped[day] ?? [])
            }
        }
    }

    // Same 3pm morning/night split and identical "orange branded day" pill
    // treatment as LaborView's own scheduleDayGroup/daypartRows — a past
    // schedule read from history should look exactly like the live
    // generation it was pulled from, not a plainer, unstyled version of it.
    private static let shiftTimeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "h:mma"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter
    }()
    private static let nightCutoffMinutes = 15 * 60  // 3:00pm

    private static func minutesFromMidnight(_ timeString: String?) -> Int? {
        guard let timeString, let date = shiftTimeFormatter.date(from: timeString.lowercased()) else { return nil }
        let comps = Calendar.current.dateComponents([.hour, .minute], from: date)
        guard let hour = comps.hour, let minute = comps.minute else { return nil }
        return hour * 60 + minute
    }

    private func scheduleDayGroup(day: String, rows: [ScheduleRow]) -> some View {
        let morning = rows.filter { (Self.minutesFromMidnight($0.shiftStart) ?? Self.nightCutoffMinutes) < Self.nightCutoffMinutes }
        let night = rows.filter { (Self.minutesFromMidnight($0.shiftStart) ?? Self.nightCutoffMinutes) >= Self.nightCutoffMinutes }

        return VStack(alignment: .leading, spacing: 8) {
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
            VStack(alignment: .leading, spacing: 12) {
                if !morning.isEmpty {
                    daypartRows(label: "MORNING", count: morning.count, rows: morning)
                }
                if !night.isEmpty {
                    daypartRows(label: "NIGHT", count: night.count, rows: night)
                }
            }
            .cavnarCard()
        }
    }

    private func daypartRows(label: String, count: Int, rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 4) {
                Image(systemName: label == "MORNING" ? "sun.max.fill" : "moon.stars.fill")
                    .font(.system(size: 10, weight: .bold))
                Text("\(label) · \(count)")
                    .font(.cavnarBody(12, weight: 800))
                    .tracking(1.1)
            }
            .foregroundStyle(Color.cavnarEmber)
            ForEach(Self.groupedByRole(rows)) { row in
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
    }
}
