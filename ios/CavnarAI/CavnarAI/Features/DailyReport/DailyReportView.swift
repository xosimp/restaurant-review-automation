import SwiftUI

/// One night's Daily Sales Report — pushed from Home's "Last night" card,
/// the list of nights, or a tapped `dsr` push.
///
/// Read top to bottom in under two minutes: the morning read, what went
/// well and what needs attention, the call-outs, tomorrow's actions — then
/// each block of the night as a card that shows one headline line
/// collapsed and its detail on tap, ending on the manager's close-out in
/// their own words. While the night is still being built the stage list
/// ticks through (polling /status every 5s); Close day starts it now, and
/// the owner can re-run a finished night.
///
/// Renders only what the payload has: a block the login may not read isn't
/// in it, an owner-only line isn't either, and a null figure is a dash.
struct DailyReportView: View {
    /// The urgency chip's status colour: amber when it is for today, none
    /// (ink) otherwise. Never the ember (B4 L7).
    static func urgencyTint(_ urgency: String?) -> Color? {
        switch urgency?.lowercased() {
        case "before_service", "today": return .cavnarAmber
        default: return nil
        }
    }

    @State private var viewModel: DailyReportViewModel
    @State private var expanded: Set<String> = ["sales"]
    @State private var confirmingRerun = false
    @State private var didLoad = false
    @State private var clock = CavnarEntranceClock()

    init(date: String?, follow: DSRFollow? = nil) {
        _viewModel = State(initialValue: DailyReportViewModel(businessDate: date, follow: follow))
    }

    private var showsProgress: Bool {
        viewModel.phase == .running || viewModel.isStarting
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if viewModel.isLoading && viewModel.report == nil && viewModel.checklist == nil {
                    loadingCard
                } else if let error = viewModel.errorMessage, viewModel.report == nil, viewModel.checklist == nil {
                    errorCard(error)
                } else {
                    if showsProgress { progressCard }
                    if viewModel.nothingYet && !showsProgress { nothingYetCard }
                    if let report = viewModel.report { reportBody(report) }
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 8)
            .padding(.bottom, 80)
        }
        .cavnarEmberRefreshable { await viewModel.load() }
        .cavnarModuleBackground()
        .navigationTitle("Daily report")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            cavnarTitleToolbar("Daily report")
            cavnarToolbarItem(placement: .topBarTrailing) {
                NavigationLink(value: DailyReportRoute.week(date: viewModel.businessDate)) {
                    Image(systemName: "tablecells")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber2)
                        .cavnarToolbarIconGlass()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("The week")
            }
        }
        .cavnarEmberBackButton()
        .task {
            guard !didLoad else { return }
            didLoad = true
            await viewModel.load()
        }
        // Restarted every time polling is asked for; cancelled when the
        // screen goes away, so nothing polls behind another screen.
        .task(id: viewModel.pollGeneration) { await viewModel.followProgress() }
        .confirmationDialog("Re-run \(viewModel.displayDate)?", isPresented: $confirmingRerun, titleVisibility: .visible) {
            Button("Re-run this night") { Task { await viewModel.closeDay(rerun: true) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Cavnar builds a new version from the latest data. This version stays in the version list.")
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            DSRKicker(text: "Daily sales report")
            title
            if let fiscal = viewModel.report?.fiscal?.label ?? viewModel.report?.facts.fiscal?.label {
                HomeMixedText.make(fiscal, size: 14, color: .cavnarInk3)
            }
            HStack(spacing: 10) {
                if viewModel.report != nil || viewModel.checklist != nil || viewModel.runStarted {
                    DSRStatusPill(phase: viewModel.phase)
                }
                versionControl
                if let closed = closedByLine {
                    Text(closed).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3).lineLimit(1)
                }
            }
            dayStepper
        }
    }

    /// The night before or after `iso` — nil for a night after `today` (on
    /// the restaurant's clock) or for anything that isn't a date.
    static func adjacentNight(_ iso: String?, by days: Int, today: String) -> String? {
        guard let iso, DSRFormat.isISODate(iso) else { return nil }
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = TimeZone(secondsFromGMT: 0)!
        let p = iso.split(separator: "-").compactMap { Int($0) }
        guard p.count == 3,
              let date = utc.date(from: DateComponents(year: p[0], month: p[1], day: p[2])),
              let moved = utc.date(byAdding: .day, value: days, to: date) else { return nil }
        let out = CavnarDate.isoDay(moved, in: utc.timeZone)
        return out > today ? nil : out
    }

    /// Previous / next night in place, and the report as text to send to a
    /// partner (friction audit #50, U3-15) — comparing Friday with Saturday
    /// was back, list, row; sharing was a screenshot.
    @ViewBuilder
    private var dayStepper: some View {
        let today = CavnarDate.isoDay(Date(), in: RestaurantClock.timeZone)
        let current = viewModel.report?.businessDate ?? viewModel.businessDate
        let previous = Self.adjacentNight(current, by: -1, today: today)
        let next = Self.adjacentNight(current, by: 1, today: today)
        HStack(spacing: 8) {
            stepButton("Previous night", systemImage: "chevron.left", to: previous)
            stepButton("Next night", systemImage: "chevron.right", to: next)
            Spacer(minLength: 0)
            if let text = shareText {
                ShareLink(item: text) {
                    Label("Share", systemImage: "square.and.arrow.up")
                        .font(.cavnarBody(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                }
            }
        }
    }

    private func stepButton(_ label: String, systemImage: String, to date: String?) -> some View {
        Button {
            guard let date else { return }
            Haptic.selection()
            expanded = ["sales"]
            viewModel = DailyReportViewModel(businessDate: date)
            Task { await viewModel.load() }
        } label: {
            Image(systemName: systemImage)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(date == nil ? Color.cavnarInk3.opacity(0.4) : Color.cavnarEmber2)
                .frame(width: 44, height: 44)
                .background(Color.cavnarEmber.opacity(date == nil ? 0.04 : 0.12), in: Circle().inset(by: 5))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(date == nil)
        .accessibilityLabel(label)
    }

    /// The night as text: the date, the morning read, what went well and
    /// what needs attention — only lines this login was sent.
    private var shareText: String? {
        guard let report = viewModel.report else { return nil }
        var lines = ["Daily sales report — \(report.displayDate)"]
        if let n = report.narrative {
            if let s = n.executiveSummary?.text, !s.isEmpty { lines.append(""); lines.append(s) }
            if !n.wentWell.isEmpty {
                lines.append("")
                lines.append("Went well")
                lines += n.wentWell.map { "• " + $0.text }
            }
            if !n.needsAttention.isEmpty {
                lines.append("")
                lines.append("Needs attention")
                lines += n.needsAttention.map { "• " + $0.text }
            }
        }
        return lines.count > 1 ? lines.joined(separator: "\n") : nil
    }

    /// "Tuesday 9/22/26" — the weekday in Clash, the date in the number face.
    private var title: Text {
        guard let date = viewModel.businessDate else {
            return Text(viewModel.displayDate).font(.cavnarHeadline(27)).foregroundStyle(Color.cavnarInk)
        }
        let weekday = DSRFormat.weekday(date).map { "\($0) " } ?? ""
        return Text(weekday).font(.cavnarHeadline(27)).foregroundStyle(Color.cavnarInk)
            + Text(viewModel.displayDate).font(.cavnarNumber(26, weight: 600)).foregroundStyle(Color.cavnarInk)
    }

    private var closedByLine: String? {
        switch viewModel.checklist?.closedBy ?? viewModel.report?.checklist?.closedBy {
        // dsr.pipeline's closed_by: pos / close_time / manual / deadline.
        case "pos": return "Closed by the POS"
        case "manual": return "Closed by hand"
        case "close_time": return "Closed at closing time"
        case "deadline": return "Closed at the deadline"
        default: return nil
        }
    }

    @ViewBuilder
    private var versionControl: some View {
        if let report = viewModel.report, report.versions.count > 1 {
            let showing = viewModel.selectedVersion ?? report.versions.map(\.version).max() ?? report.version ?? 1
            Menu {
                ForEach(report.versions.sorted { $0.version > $1.version }) { v in
                    Button {
                        Task { await viewModel.selectVersion(v.version) }
                    } label: {
                        if v.version == showing {
                            Label("Version \(v.version) · \(v.phase.label)", systemImage: "checkmark")
                        } else {
                            Text("Version \(v.version) · \(v.phase.label)")
                        }
                    }
                }
            } label: {
                HStack(spacing: 4) {
                    HomeMixedText.make("Version \(showing) of \(report.versions.count)", size: 12.5, weight: 700,
                                       color: .cavnarEmber2)
                    Image(systemName: "chevron.down").font(.system(size: 10, weight: .bold)).foregroundStyle(Color.cavnarEmber2)
                }
            }
            .accessibilityLabel("Version \(showing) of \(report.versions.count). Choose a version")
        }
    }

    // MARK: - States

    private var loadingCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarSkeletonLines(widths: [1, 0.92, 0.7])
            CavnarSkeletonLines(widths: [0.85, 0.6], lineHeight: 10)
        }
        .cavnarCard(.ai)
    }

    private func errorCard(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(message).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarRed)
            Button("Try again") { Task { await viewModel.load() } }
                .buttonStyle(CavnarSecondaryButtonStyle())
        }
        .cavnarCard()
    }

    private var progressCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            DSRKicker(text: viewModel.isStarting ? "Starting" : "Building the report")
            Text((viewModel.isStarting ? nil : viewModel.checklist?.statusLabel) ?? "Starting the night")
                .font(.cavnarHeadline(19))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            // While a re-run's new version hasn't appeared, the stage list
            // on hand is the finished one's — not what is running.
            DSRProgressChecklist(checklist: viewModel.isStarting ? nil : viewModel.checklist,
                                 starting: viewModel.isStarting)
            if viewModel.pollGaveUp {
                Text("Still going. Pull down to check again.")
                    .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
            }
            closeDayControl
        }
        .cavnarCard(.hero)
    }

    private var nothingYetCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(viewModel.businessDate == nil ? "No daily reports yet" : "Nothing for \(viewModel.displayDate) yet")
                .font(.cavnarHeadline(19))
                .foregroundStyle(Color.cavnarInk)
            Text("The report builds itself after close. Close the day to build it now.")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            closeDayControl
        }
        .cavnarCard()
    }

    @ViewBuilder
    private var closeDayControl: some View {
        if viewModel.canCloseDay {
            Button {
                Haptic.medium()
                Task { await viewModel.closeDay() }
            } label: {
                Group {
                    if viewModel.isSubmitting { CavnarShimmerText(text: "Closing the day") } else { Text("Close day") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSubmitting))
            .disabled(viewModel.isSubmitting)
        }
        if let error = viewModel.actionError {
            Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - The report

    @ViewBuilder
    private func reportBody(_ report: DSRReport) -> some View {
        if !report.facts.missing.isEmpty {
            CavnarCaveat(title: report.phase == .provisional ? "Provisional — still missing" : "Still missing",
                         detail: report.facts.missing.joined(separator: " "))
        }
        if let narrative = report.narrative, !narrative.isEmpty {
            narrativeSections(narrative)
        } else if report.phase.isTerminal {
            Text(noSummaryLine(report))
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
                .cavnarCard()
        }

        if !report.orderedBlocks.isEmpty {
            HomeSectionHeader(kicker: "The night", title: "Block by block")
                .padding(.top, 8)
            ForEach(Array(report.orderedBlocks.enumerated()), id: \.element.name) { index, entry in
                blockCard(entry.name, entry.block)
                    .cavnarRowEntrance(index: index, clock: clock)
            }
        }

        footer(report)
    }

    private func noSummaryLine(_ report: DSRReport) -> String {
        if let reason = report.checklist?.narrative?.reason, !reason.isEmpty {
            return "No written summary for this night — \(reason). Every figure below is still measured."
        }
        return "No written summary for this night. Every figure below is still measured."
    }

    @ViewBuilder
    private func narrativeSections(_ n: DSRNarrative) -> some View {
        if let summary = n.executiveSummary {
            VStack(alignment: .leading, spacing: 10) {
                DSRKicker(text: "The morning read")
                HomeMixedText.make(summary.text, size: 16.5, weight: 500, color: .cavnarInk)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard(.ai)
        }
        if !n.wentWell.isEmpty {
            titledCard("Went well") { DSRLineList(lines: n.wentWell, dot: .cavnarGreen) }
        }
        if !n.needsAttention.isEmpty {
            titledCard("Needs attention") { DSRLineList(lines: n.needsAttention, dot: .cavnarAmber) }
        }
        if !n.callouts.isEmpty {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10, alignment: .top),
                                GridItem(.flexible(), spacing: 10, alignment: .top)], spacing: 10) {
                ForEach(n.callouts, id: \.label) { c in
                    VStack(alignment: .leading, spacing: 6) {
                        DSRKicker(text: c.label)
                        HomeMixedText.make(c.line.text, size: 13.5, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    .cavnarCard()
                }
            }
        }
        if !n.actionsTomorrow.isEmpty {
            titledCard("Tomorrow") {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(Array(n.actionsTomorrow.enumerated()), id: \.element.id) { i, action in
                        HStack(alignment: .top, spacing: 12) {
                            Text("\(i + 1)")
                                .font(.cavnarNumber(16, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                                .frame(width: 18, alignment: .leading)
                            VStack(alignment: .leading, spacing: 6) {
                                HomeMixedText.make(action.text, size: 15, weight: 700, color: .cavnarInk)
                                    .fixedSize(horizontal: false, vertical: true)
                                if let why = action.why {
                                    HomeMixedText.make(why, size: 13.5, color: .cavnarInk3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                let chips = [action.urgencyLabel, action.effortLabel].compactMap { $0 }
                                if !chips.isEmpty {
                                    AccountFlowLayout(spacing: 6) {
                                        ForEach(Array(chips.enumerated()), id: \.offset) { j, chip in
                                            // The urgency is a status: amber for today, ink
                                            // otherwise — never the ember (B4 L7).
                                            AccountChip(text: chip, muted: true,
                                                        tint: (j == 0 && action.urgencyLabel != nil)
                                                            ? DailyReportView.urgencyTint(action.urgency) : nil)
                                        }
                                    }
                                }
                                // "Today" the cited facts didn't carry, moved
                                // and said why (H13).
                                if let moved = action.urgencyAdjustedLine {
                                    HomeMixedText.make(moved + ".", size: 12.5, color: .cavnarInk3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                // The dollars, calibrated by measured results
                                // when the server corrected them (F6).
                                if let dollars = action.dollarsLine {
                                    HomeMixedText.make(dollars, size: 13, weight: 600, color: .cavnarInk2)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                // How sure, from the facts it cites (K1).
                                if let c = action.confidence {
                                    ConfidenceLine(confidence: c, recKey: action.answerKey,
                                                   surface: "dsr", module: action.answerModule)
                                }
                                // Done / Not for us / Track, as on the web
                                // (rec-ROI #9). An answered action keeps its
                                // line — the report is a record — and drops
                                // the controls.
                                if action.showsAnswers, let key = action.answerKey {
                                    RecAnswerRow(key: key, surface: "dsr", module: action.answerModule)
                                        .padding(.top, 2)
                                }
                            }
                            Spacer(minLength: 0)
                        }
                    }
                }
            }
        }
        // Kept of checked, and — as the web says — how many were dropped
        // because a figure didn't trace, and any estimates counted apart.
        if let footer = n.verification?.footer {
            HomeMixedText.make(footer, size: 12, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func titledCard<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title).font(.cavnarHeadline(19)).foregroundStyle(Color.cavnarInk)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func blockCard(_ name: String, _ block: DSRBlock) -> some View {
        DSRBlockCard(title: DSRBlock.titles[name] ?? name.capitalized, block: block,
                     headline: DSRHeadline.line(for: name, block),
                     isExpanded: Binding(get: { expanded.contains(name) },
                                         set: { if $0 { expanded.insert(name) } else { expanded.remove(name) } })) {
            DSRBlockBody(name: name, block: block, businessDate: viewModel.businessDate)
        }
    }

    @ViewBuilder
    private func footer(_ report: DSRReport) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if viewModel.canRerun {
                Button {
                    Haptic.light()
                    confirmingRerun = true
                } label: {
                    Group {
                        if viewModel.isSubmitting { CavnarShimmerText(text: "Starting", color: .cavnarInk) } else { Text("Re-run this night") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.isSubmitting)
                if !showsProgress, let error = viewModel.actionError {
                    Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            NavigationLink(value: DailyReportRoute.week(date: viewModel.businessDate)) {
                HStack(spacing: 6) {
                    Text("See the week").font(.cavnarBody(14, weight: 700))
                    Image(systemName: "chevron.right").font(.system(size: 11, weight: .bold))
                }
                .foregroundStyle(Color.cavnarEmber2)
            }
            .buttonStyle(.plain)
        }
        .padding(.top, 6)
    }
}

// MARK: - Block bodies

/// What each block shows when opened — only the parts the payload carries.
struct DSRBlockBody: View {
    let name: String
    let block: DSRBlock
    var businessDate: String?

    var body: some View {
        switch name {
        case "sales": sales
        case "labor": labor
        case "food": food
        case "reviews": reviews
        case "marketing": marketing
        case "intel": intel
        case "closeout": closeout
        default: EmptyView()
        }
    }

    private func m(_ key: String) -> Double? { block.metric(key) }

    private func joined(_ parts: [String?]) -> String? {
        let kept = parts.compactMap { $0 }
        return kept.isEmpty ? nil : kept.joined(separator: " \u{00B7} ")
    }

    // Sales

    private var sales: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                DSRKicker(text: "Net sales")
                Text(DSRFormat.money(m("net")))
                    .font(.cavnarNumber(40, weight: 600))
                    .foregroundStyle(m("net") == nil ? Color.cavnarInk3 : Color.cavnarInk)
                    .cavnarNumberGlow()
                    .cavnarSensitive()
                if let line = joined([
                    // An "all" gross the POS couldn't complete says so
                    // here, never the items figure passed off as it.
                    m("gross").map { "Gross \(DSRFormat.money($0))" } ?? block.grossNotMeasured,
                    m("transactions").map { "\(DSRFormat.count($0)) checks" },
                    m("guests").map { "\(DSRFormat.count($0)) guests" },
                    m("avg_ticket").map { "\(DSRFormat.money($0)) avg check" },
                ]) {
                    HomeMixedText.make(line, size: 13.5, color: .cavnarInk3)
                }
                if let budget = joined([
                    block.has("budget_net") ? "\(DSRFormat.money(m("budget_net"))) net" : nil,
                    block.has("budget_gross") ? "\(DSRFormat.money(m("budget_gross"))) gross" : nil,
                ]) {
                    HomeMixedText.make("Budget " + budget, size: 13.5, color: .cavnarInk3)
                }
            }

            let comparisons = salesComparisons
            if !comparisons.isEmpty {
                VStack(spacing: 0) {
                    ForEach(Array(comparisons.enumerated()), id: \.offset) { i, c in
                        HStack {
                            Text(c.label).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                            Spacer()
                            Text(DSRFormat.signedPct(c.pct))
                                .font(.cavnarNumber(14, weight: 700))
                                .foregroundStyle(DSRFormat.tone(c.pct))
                            Text(DSRFormat.money(c.base))
                                .font(.cavnarNumber(14))
                                .foregroundStyle(Color.cavnarInk3)
                                .frame(width: 76, alignment: .trailing)
                        }
                        .padding(.vertical, 8)
                        .accessibilityElement(children: .combine)
                        if i < comparisons.count - 1 { AccountRowDivider() }
                    }
                }
            }

            let cats = block.categories
            if !cats.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    DSRKicker(text: "By category", tone: .cavnarInk3)
                    DSRCategoryBars(categories: cats)
                }
            }

            let hours = block.hourly
            if !hours.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    DSRKicker(text: "By hour", tone: .cavnarInk3)
                    DSRHourlyBars(hours: hours)
                }
            }

            let items = block.topItems
            if !items.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    DSRKicker(text: "Top items", tone: .cavnarInk3)
                    ForEach(Array(items.prefix(5).enumerated()), id: \.offset) { _, item in
                        HStack {
                            Text(item.name).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2).lineLimit(1)
                            Spacer()
                            Text(DSRFormat.count(item.qty)).font(.cavnarNumber(14)).foregroundStyle(Color.cavnarInk3)
                            Text(DSRFormat.money(item.net)).font(.cavnarNumber(14, weight: 600)).foregroundStyle(Color.cavnarInk)
                                .frame(width: 76, alignment: .trailing)
                        }
                    }
                }
            }

            // Discounts always; comps, voids and refunds only when this
            // login may see them (the server drops them otherwise).
            // Tax last, captioned with where it sits for this restaurant's
            // gross (the web's tile says the same).
            let loss = [("Discounts", "discounts"), ("Comps", "comps"), ("Voids", "voids"), ("Refunds", "refunds")]
                .filter { block.has($0.1) }
                .map { DSRStatTile(label: $0.0, value: DSRFormat.money(m($0.1))) }
                + (m("tax").map { [DSRStatTile(label: "Tax collected", value: DSRFormat.money($0),
                                               detail: block.taxCaption)] } ?? [])
            if !loss.isEmpty { DSRTileRow(tiles: loss) }

            if let note = block.definitionNote {
                HomeMixedText.make(note, size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var salesComparisons: [(label: String, pct: Double?, base: Double?)] {
        let lastWeekLabel = businessDate.flatMap(DSRFormat.weekday).map { "vs last \($0)" } ?? "vs last week"
        let all: [(String, String, String)] = [
            ("vs yesterday", "vs_yesterday_pct", "yesterday_net"),
            (lastWeekLabel, "vs_last_week_pct", "last_week_net"),
            ("vs last year", "vs_last_year_pct", "last_year_net"),
            ("vs forecast", "vs_forecast_pct", "forecast_net"),
            ("vs budget", "vs_budget_net_pct", "budget_net"),
        ]
        // A comparison whose baseline was never measured says nothing —
        // leave the row out rather than show two dashes.
        return all.filter { m($0.2) != nil }.map { ($0.0, m($0.1), m($0.2)) }
    }

    // Labor

    private var labor: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(DSRFormat.pct(m("pct")))
                    .font(.cavnarNumber(34, weight: 600))
                    .foregroundStyle(laborTone)
                if let line = joined([
                    "of net sales",
                    m("target_pct").map { "target \(DSRFormat.pct($0))" },
                    m("vs_target_pts").map { DSRFormat.signedPoints($0) },
                ]) {
                    HomeMixedText.make(line, size: 13.5, color: .cavnarInk3)
                }
            }
            DSRTileRow(tiles: [
                DSRStatTile(label: "Labor cost", value: DSRFormat.money(m("cost"))),
                DSRStatTile(label: "Hours", value: DSRFormat.count(m("hours"))),
                DSRStatTile(label: "Overtime", value: m("overtime_hours").map { "\(DSRFormat.count($0)) hrs" } ?? DSRFormat.dash),
            ] + [("No-shows", "no_shows"), ("Late", "late_arrivals")].compactMap { label, key in
                m(key).map { DSRStatTile(label: label, value: DSRFormat.count($0)) }
            })
            if !block.observations.isEmpty {
                DSRLineList(lines: block.observations.map { DSRLine(text: $0) }, dot: .cavnarInk3)
            }
            if let note = block.coverageNote {
                Text(note).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    /// The food block's drivers total, under its new name or its old one,
    /// with its detail's basis and completeness.
    private var atStakeLine: String? {
        let keys = ["drivers_at_stake_monthly", "at_stake_monthly", "recoverable_monthly"]
        guard let key = keys.first(where: { m($0) != nil }), let amount = m(key) else { return nil }
        let detail = block.detail["drivers_at_stake"] ?? block.detail["at_stake"] ?? block.detail["recoverable"]
        return OwnerCopy.dsrAtStakeLine(amount: DSRFormat.money(amount), basis: detail?["basis"]?.string,
                                        complete: detail?["complete"]?.bool)
    }

    /// Red over target, green under it, plain ink when there's no target.
    private var laborTone: Color {
        guard m("pct") != nil else { return .cavnarInk3 }
        guard let pts = m("vs_target_pts"), pts != 0 else { return .cavnarInk }
        return pts > 0 ? .cavnarRed : .cavnarGreen
    }

    // Food

    private var food: some View {
        VStack(alignment: .leading, spacing: 14) {
            DSRTileRow(tiles: [
                DSRStatTile(label: "Est. food cost", value: DSRFormat.pct(m("est_food_cost_pct")),
                            detail: m("est_food_cost").map { DSRFormat.money($0) }),
                DSRStatTile(label: "Waste logged", value: DSRFormat.money(m("waste_logged")),
                            detail: m("waste_inferred").map { "\(DSRFormat.money($0)) inferred" }),
                DSRStatTile(label: "Variance", value: DSRFormat.money(m("variance_cost")),
                            detail: m("variance_items").map { "\(DSRFormat.count($0)) item\($0 == 1 ? "" : "s")" }),
            ])
            // The % withheld under the coverage floor, and why (I11) — the
            // dash above is not a zero.
            if let note = block.foodCoverageNote {
                HomeMixedText.make(note, size: 13, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // The cost drivers' monthly total is an opportunity, never money
            // recovered: it carries its basis, and "partial" when a source
            // was missing (NS1 #7). The renamed key is read first.
            if let line = atStakeLine {
                HomeMixedText.make(line, size: 14, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            let stock = block.criticalStock
            if !stock.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    DSRKicker(text: "Critically low", tone: .cavnarInk3)
                    ForEach(stock, id: \.self) { s in
                        HStack {
                            Text(s.item).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                            Spacer()
                            Text(s.daysRemaining.map { "\(DSRFormat.count($0)) days left" } ?? DSRFormat.dash)
                                .font(.cavnarNumber(13.5))
                                .foregroundStyle((s.daysRemaining ?? 1) < 1 ? Color.cavnarRed : Color.cavnarAmber)
                        }
                    }
                    if let basis = block.stockBasis {
                        Text(basis).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    }
                }
            }
            let variance = block.varianceItems
            if !variance.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    DSRKicker(text: "Usage over recipe", tone: .cavnarInk3)
                    ForEach(variance, id: \.self) { v in
                        HStack {
                            Text(v.dish.map { "\(v.ingredient) · \($0)" } ?? v.ingredient)
                                .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                            Spacer()
                            Text(DSRFormat.money(v.cost)).font(.cavnarNumber(14, weight: 600)).foregroundStyle(Color.cavnarInk)
                        }
                    }
                    if let window = block.varianceWindow {
                        HomeMixedText.make(window, size: 12, color: .cavnarInk3)
                    }
                }
            }
            if let label = block.estimateLabel {
                CavnarCaveat(title: "Estimated", detail: label)
            }
        }
    }

    // Reviews

    private var reviews: some View {
        VStack(alignment: .leading, spacing: 14) {
            DSRTileRow(tiles: [
                DSRStatTile(label: "Received", value: DSRFormat.count(m("received"))),
                // No night rating under the floor (I11): the dash says why.
                DSRStatTile(label: "Average", value: DSRFormat.rating(m("avg_rating")),
                            detail: m("avg_rating") == nil ? block.ratingNote : nil),
                DSRStatTile(label: "Urgent", value: DSRFormat.count(m("urgent")),
                            tone: (m("urgent") ?? 0) > 0 ? .cavnarRed : .cavnarInk),
            ])
            let rows = block.reviewRows
            if !rows.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(rows.enumerated()), id: \.offset) { i, r in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(DSRFormat.rating(r.rating))
                                .font(.cavnarNumber(13.5, weight: 700))
                                .foregroundStyle((r.rating ?? 5) <= 2 ? Color.cavnarRed : Color.cavnarInk2)
                                .frame(width: 40, alignment: .leading)
                            Text(r.summary).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                            if r.urgent {
                                Text("Urgent").font(.cavnarBody(11.5, weight: 700)).foregroundStyle(Color.cavnarRed)
                            }
                        }
                        .padding(.vertical, 8)
                        if i < rows.count - 1 { AccountRowDivider() }
                    }
                }
            }
            if let note = block.syncNote {
                Text(note).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    // Marketing

    private var marketing: some View {
        VStack(alignment: .leading, spacing: 14) {
            DSRTileRow(tiles: [
                DSRStatTile(label: "Posts", value: DSRFormat.count(m("posts_published")),
                            detail: m("reach").map { "\(DSRFormat.count($0)) reach" }),
                DSRStatTile(label: "Engagement", value: DSRFormat.pct(m("engagement_rate"))),
                DSRStatTile(label: "Texts sent", value: DSRFormat.count(m("texts_sent"))),
            ])
            let posts = block.posts
            if !posts.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    DSRKicker(text: "Posted", tone: .cavnarInk3)
                    ForEach(posts, id: \.self) { p in
                        HStack(alignment: .firstTextBaseline) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(p.topic).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                                if let sub = joined([p.platform?.capitalized, p.at]) {
                                    HomeMixedText.make(sub, size: 12, color: .cavnarInk3)
                                }
                            }
                            Spacer()
                            Text(p.reach.map { "\(DSRFormat.count($0)) reach" } ?? DSRFormat.dash)
                                .font(.cavnarNumber(13)).foregroundStyle(Color.cavnarInk3)
                        }
                    }
                }
            }
            let texts = block.textCampaigns
            if !texts.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    DSRKicker(text: "Texts", tone: .cavnarInk3)
                    ForEach(texts, id: \.self) { t in
                        HStack(alignment: .firstTextBaseline) {
                            HomeMixedText.make(t.message, size: 14, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer()
                            Text(t.sent.map { "\(DSRFormat.count($0)) sent" } ?? DSRFormat.dash)
                                .font(.cavnarNumber(13)).foregroundStyle(Color.cavnarInk3)
                        }
                    }
                }
            }
        }
    }

    // Intel

    private var intel: some View {
        VStack(alignment: .leading, spacing: 12) {
            DSRTileRow(tiles: [
                DSRStatTile(label: "High / low",
                            value: m("weather_high_f") == nil && m("weather_low_f") == nil
                                ? DSRFormat.dash
                                : "\(DSRFormat.degrees(m("weather_high_f"))) / \(DSRFormat.degrees(m("weather_low_f")))"),
                DSRStatTile(label: "Rain", value: DSRFormat.pct(m("weather_precip_pct"))),
                DSRStatTile(label: "Covers booked", value: DSRFormat.count(m("reservations_covers"))),
            ])
            if let events = block.eventsSummary {
                HomeMixedText.make("Events: \(events)", size: 14, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let note = block.weatherNote {
                Text(note).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let note = block.competitorsNote {
                Text(note).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    // Close-out — verbatim.

    private var closeout: some View {
        VStack(alignment: .leading, spacing: 0) {
            if block.detail["verbatim"]?.bool == true {
                Text("In their own words, never edited.")
                    .font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                    .padding(.bottom, 8)
            }
            let fields = block.closeoutFields
            if fields.isEmpty {
                Text("Nothing was written.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
            }
            ForEach(Array(fields.enumerated()), id: \.element.key) { i, f in
                VStack(alignment: .leading, spacing: 4) {
                    DSRKicker(text: f.label)
                    Text(f.text)
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 9)
                if i < fields.count - 1 { AccountRowDivider() }
            }
        }
    }
}
