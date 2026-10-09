import SwiftUI

/// One night's Daily Sales Report — pushed from Home's "Last night" card,
/// the list of nights, or a tapped `dsr` push.
///
/// "Web explains. iPhone decides." (iOS readability round, 10/8/26): the
/// score, then the one thing to do, then the night in one sentence with
/// the rest a tap away; the remaining priorities, the day after, four key
/// numbers, and each block of the night as a card that shows one line
/// collapsed and its detail on tap. While the night is still being built
/// the stage list ticks through (polling /status every 5s); Close day
/// starts it now, and the owner can re-run a finished night. Previous /
/// next night and Share sit in thumb reach at the bottom, and a sideways
/// swipe moves between nights.
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
    // Every block starts closed (owner decision 9/25/26): each card's one
    // line already carries its number, and the score card above has the
    // net — the Sales block open by default restated it.
    @State private var expanded: Set<String> = []
    /// "All KPIs" / "All priorities" (density #2).
    @State private var showingAllKPIs = false
    @State private var showingAllPriorities = false
    @State private var confirmingRerun = false
    @State private var didLoad = false
    /// An unmapped POS department the owner is placing (parity audit #33).
    @State private var mapping: DSRBlock.Unmapped?
    /// "The night in full": insights, every number and the blocks, folded
    /// under the key numbers (re-audit D9).
    @State private var showingNightInFull = false
    @State private var clock = CavnarEntranceClock()
    /// The hour chart's scrub, so a drag along it never swipes the night.
    @State private var scrub = DSRScrubState()

    init(date: String?, follow: DSRFollow? = nil) {
        _viewModel = State(initialValue: DailyReportViewModel(businessDate: date, follow: follow))
    }

    private var showsProgress: Bool {
        viewModel.phase == .running || viewModel.isStarting
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: CavnarSpace.m) {
                header
                if viewModel.isLoading && viewModel.report == nil && viewModel.checklist == nil {
                    loadingCard
                } else if let error = viewModel.errorMessage, viewModel.report == nil, viewModel.checklist == nil {
                    errorCard(error)
                } else {
                    // Switched off for this location (parity audit #64):
                    // past nights still read; nothing new is built.
                    if !viewModel.enabled {
                        CavnarCaveat(title: "Switched off", detail: DSRAvailability.offLine)
                    }
                    if showsProgress { progressCard }
                    if viewModel.nothingYet && !showsProgress { nothingYetCard }
                    if let report = viewModel.report { reportBody(report) }
                }
            }
            .padding(.horizontal, CavnarSpace.gutter)
            .padding(.top, CavnarSpace.xs)
            .padding(.bottom, CavnarSpace.xl)
            // The night reads as one column on an iPad (#99).
            .cavnarReadableWidth()
        }
        .environment(scrub)
        // A sideways swipe moves between nights (10/8/26). Simultaneous, so
        // the page still scrolls; never from the screen's left edge (the
        // back swipe) nor while the hour chart is being scrubbed.
        .simultaneousGesture(nightSwipe)
        .cavnarEmberRefreshable { await viewModel.load() }
        .cavnarPinnedBar { nightBar }
        .cavnarModuleBackground()
        .navigationTitle("Daily report")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            cavnarTitleToolbar("Daily report")
            // The report's settings are the web's (re-audit D13, "Web
            // explains. iPhone decides."): a link at the foot of the report;
            // "Map it" stays on the night itself.
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
            // Versions, how the day was closed and Re-run — what the header
            // used to carry (10/8/26).
            if hasMoreMenu {
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Menu { moreMenu } label: {
                        Image(systemName: "ellipsis")
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber2)
                            .cavnarToolbarIconGlass()
                    }
                    .accessibilityLabel("More about this night")
                }
            }
        }
        .cavnarEmberBackButton()
        .sheet(item: $mapping, onDismiss: { Task { await viewModel.load() } }) { dept in
            DSRCategoryMapSheet(department: dept)
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
        .task {
            guard !didLoad else { return }
            didLoad = true
            await viewModel.load()
        }
        // Restarted every time polling is asked for; cancelled when the
        // screen goes away, so nothing polls behind another screen.
        .task(id: viewModel.pollGeneration) { await viewModel.followProgress() }
        .confirmationDialog(DSRCloseGate.question,
                            isPresented: Binding(get: { viewModel.confirmingEarlyClose },
                                                 set: { viewModel.confirmingEarlyClose = $0 }),
                            titleVisibility: .visible) {
            Button("Close it anyway") { Task { await viewModel.closeDay(early: true) } }
            Button("Cancel", role: .cancel) {}
        }
        .confirmationDialog("Re-run \(viewModel.displayDate)?", isPresented: $confirmingRerun, titleVisibility: .visible) {
            Button("Re-run this night") { Task { await viewModel.closeDay(rerun: true) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Cavnar AI builds a new version from the latest data. This version stays in the version list.")
        }
    }

    // MARK: - Header

    /// The night's name, its fiscal week, and a status only when the night
    /// is not Final (10/8/26: no kicker repeating the screen's title; the
    /// version and how the day was closed are in the toolbar's "…").
    private var header: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            title
                .lineLimit(2)
                .minimumScaleFactor(0.85)
                .accessibilityAddTraits(.isHeader)
            if let fiscal = viewModel.report?.fiscal?.label ?? viewModel.report?.facts.fiscal?.label {
                HomeMixedText.make(fiscal, role: .secondary)
            }
            if showsStatus {
                DSRStatusPill(phase: viewModel.phase)
                    .padding(.top, CavnarSpace.xxs)
            }
        }
    }

    private var showsStatus: Bool {
        (viewModel.report != nil || viewModel.checklist != nil || viewModel.runStarted) && viewModel.phase != .final
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

    /// The nights either side of the one on screen.
    private var neighbours: (previous: String?, next: String?) {
        let today = CavnarDate.isoDay(Date(), in: RestaurantClock.timeZone)
        let current = viewModel.report?.businessDate ?? viewModel.businessDate
        return (Self.adjacentNight(current, by: -1, today: today), Self.adjacentNight(current, by: 1, today: today))
    }

    /// Previous / next night and the report as text to send to a partner
    /// (friction audit #50, U3-15), pinned in thumb reach (10/8/26).
    @ViewBuilder
    private var nightBar: some View {
        let n = neighbours
        stepButton(n.previous, forward: false)
        Spacer(minLength: 0)
        if let text = shareText {
            ShareLink(item: text) {
                Label("Share", systemImage: "square.and.arrow.up")
                    .font(.cavnarBody(CavnarType.body, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .cavnarHitTarget()
            }
        }
        Spacer(minLength: 0)
        stepButton(n.next, forward: true)
    }

    private func stepButton(_ date: String?, forward: Bool) -> some View {
        Button {
            go(to: date)
        } label: {
            HStack(spacing: CavnarSpace.xxs) {
                if !forward { Image(systemName: "chevron.left").font(.cavnar(.secondary)) }
                Text(date.flatMap { DSRFormat.weekday($0) } ?? (forward ? "Next" : "Previous"))
                    .font(.cavnarBody(CavnarType.body, weight: 700))
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                if forward { Image(systemName: "chevron.right").font(.cavnar(.secondary)) }
            }
            .foregroundStyle(Color.cavnarEmber2)
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        // No night after tonight: the control isn't there (a dimmed label
        // would be Ink3 on a button).
        .opacity(date == nil ? 0 : 1)
        .disabled(date == nil)
        .accessibilityHidden(date == nil)
        .accessibilityLabel(forward ? "Next night" : "Previous night")
    }

    private func go(to date: String?) {
        guard let date else { return }
        Haptic.selection()
        expanded = []
        showingAllKPIs = false
        showingAllPriorities = false
        viewModel = DailyReportViewModel(businessDate: date)
        Task { await viewModel.load() }
    }

    private var nightSwipe: some Gesture {
        DragGesture(minimumDistance: 30, coordinateSpace: .global)
            .onEnded { v in
                guard !scrub.recentlyScrubbed, v.startLocation.x > 32 else { return }
                let dx = v.translation.width, dy = v.translation.height
                guard abs(dx) > 90, abs(dx) > abs(dy) * 2 else { return }
                let n = neighbours
                go(to: dx < 0 ? n.next : n.previous)
            }
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

    /// "Tuesday 9/22/26" — the weekday in Clash, the date in the number face,
    /// both at the Title role.
    private var title: Text {
        let dateFont = Font.cavnarNumber(CavnarText.title.size, weight: 600, relativeTo: CavnarText.title.textStyle)
        guard let date = viewModel.businessDate else {
            return Text(viewModel.displayDate).font(.cavnar(.title)).foregroundColor(.cavnarInk)
        }
        let weekday = DSRFormat.weekday(date).map { "\($0) " } ?? ""
        return Text(weekday).font(.cavnar(.title)).foregroundColor(.cavnarInk)
            + Text(viewModel.displayDate).font(dateFont).foregroundColor(.cavnarInk)
    }

    /// How the day was closed, in an owner's words.
    private var closedByLine: String? {
        switch viewModel.checklist?.closedBy ?? viewModel.report?.checklist?.closedBy {
        // dsr.pipeline's closed_by: pos / close_time / manual / deadline.
        case "pos": return "Closed when the register closed"
        case "manual": return "Closed by hand"
        case "close_time": return "Closed at closing time"
        case "deadline": return "Closed at the report deadline"
        default: return nil
        }
    }

    private var hasMoreMenu: Bool {
        (viewModel.report?.versions.count ?? 0) > 1 || closedByLine != nil || viewModel.canRerun
    }

    /// "Latest, built 9/23/26 · 7:10am · Final" — never "Version 2 of 3".
    private func versionLabel(_ v: DSRVersion, latest: Int) -> String {
        let built = v.createdAt.map { CavnarDate.mdyTimeLocal($0, in: RestaurantClock.timeZone) }
        let which = v.version == latest ? "Latest" : "Earlier"
        return which + (built.map { ", built \($0)" } ?? "") + " \u{00B7} " + v.phase.label
    }

    @ViewBuilder
    private var moreMenu: some View {
        if let report = viewModel.report, report.versions.count > 1 {
            let latest = report.versions.map(\.version).max() ?? report.version ?? 1
            let showing = viewModel.selectedVersion ?? latest
            Section("Versions of this night") {
                ForEach(report.versions.sorted { $0.version > $1.version }) { v in
                    Button {
                        Task { await viewModel.selectVersion(v.version) }
                    } label: {
                        if v.version == showing {
                            Label(versionLabel(v, latest: latest), systemImage: "checkmark")
                        } else {
                            Text(versionLabel(v, latest: latest))
                        }
                    }
                }
            }
        }
        if let closed = closedByLine {
            Section { Text(closed) }
        }
        if viewModel.canRerun {
            Button {
                confirmingRerun = true
            } label: {
                Label("Re-run this night", systemImage: "arrow.clockwise")
            }
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
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            Text(message).cavnarText(.body, color: .cavnarRedText)
            Button("Try again") { Task { await viewModel.load() } }
                .buttonStyle(CavnarSecondaryButtonStyle())
        }
        .cavnarCard()
    }

    private var progressCard: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(viewModel.isStarting ? "Starting" : "Building the report")
            Text((viewModel.isStarting ? nil : viewModel.checklist?.statusLabel) ?? "Starting the night")
                .cavnarText(.headline)
                .fixedSize(horizontal: false, vertical: true)
            // While a re-run's new version hasn't appeared, the stage list
            // on hand is the finished one's — not what is running.
            DSRProgressChecklist(checklist: viewModel.isStarting ? nil : viewModel.checklist,
                                 starting: viewModel.isStarting)
            if viewModel.pollGaveUp {
                Text("Still going. Pull down to check again.").cavnarText(.secondary)
            }
            closeDayControl
        }
        .cavnarCard(.hero)
    }

    private var nothingYetCard: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            Text(viewModel.businessDate == nil ? "No daily reports yet" : "Nothing for \(viewModel.displayDate) yet")
                .cavnarText(.headline)
            Text("The report builds itself after close. Close the day to build it now.")
                .cavnarText(.body)
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
                    if viewModel.isSubmitting { CavnarShimmerText(text: "Closing the day") } else { Text(viewModel.closeDayLabel) }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSubmitting))
            .disabled(viewModel.isSubmitting)
        }
        if let error = viewModel.actionError {
            Text(error).cavnarText(.secondary, color: .cavnarRedText)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - The report

    @ViewBuilder
    private func reportBody(_ report: DSRReport) -> some View {
        let actions = report.narrative?.actionsTomorrow ?? []
        if let missing = Self.missingLine(report) {
            CavnarCaveat(title: "Still missing", detail: missing)
        }
        // SCORE FIRST (owner decision 9/25/26; DESIGN_SYSTEM.md §12), then
        // the one thing to do (10/8/26), then the summary — its first
        // sentence, the rest a tap away — and the night's risks and wins in
        // one card, the remaining priorities, the day after, four KPIs, "The
        // night in full" folded (insights, every number, the blocks) and how
        // the night was checked. A manager's view
        // has no scorecard: the operations summary leads, then the shift,
        // the one thing, the priorities, and the numbers.
        if let card = report.scorecard {
            DSRScorecardCard(card: card, sales: report.facts.blocks["sales"])
            if let first = actions.first { DSRDoThisToday(action: first) }
        }
        leadCard(report)
        if let card = report.scorecard {
            DSRWinsRisks(card: card)
        }
        if !report.isOwnerView, let shift = report.shift {
            DSRShiftCard(shift: shift, dayName: DSRFormat.weekday(report.businessDate))
        }
        if report.scorecard == nil {
            if let first = actions.first { DSRDoThisToday(action: first) }
            if let n = report.narrative, !(n.wentWell.isEmpty && n.needsAttention.isEmpty) {
                DSRWinsRisksCard(riskTitle: "Needs attention", risks: n.needsAttention.map(\.text),
                                 winTitle: "Went well", wins: n.wentWell.map(\.text))
            }
        }
        if let n = report.narrative { priorities(n, report: report) }
        if let t = report.tomorrow {
            DSRTomorrowCard(tomorrow: t, staffingPointer: staffingPointer(report))
            // The day after's labor % and the week's overtime, while the
            // schedule can still change (parity audit #33).
            DSRTomorrowLaborCard(tomorrow: t, isOwner: report.isOwnerView)
        }

        // Four KPIs (the owner's `kpis_headline`: without the score's
        // components), every KPI behind "All numbers" — for a manager, labor
        // against target leads (it is in `kpis`), then Operations.
        let split = Self.kpiSplit(top: report.topKPIs, all: report.kpis, showingAll: false)
        DSRKPIGrid(title: "Key numbers", kpis: split.shown)
        if !report.isOwnerView {
            DSRKPIGrid(title: "Operations", kpis: report.operations)
        }

        // The rest of the night — what Cavnar AI noticed, every number and
        // each block — folded under one disclosure (re-audit D9): the
        // decision is above, the detail one tap away.
        let blocks = report.displayedBlocks.filter { $0.block != DSRBlock.notCollected }
        let foldedKPIs = max(0, report.kpis.count - split.shown.count)
        if Self.hasNightInFull(report, blocks: blocks.count, hiddenKPIs: foldedKPIs) {
            disclosureButton(showingNightInFull ? "Less of the night" : "The night in full",
                             open: showingNightInFull) { showingNightInFull.toggle() }
            if showingNightInFull {
                nightInFull(report, blocks: blocks, hiddenKPIs: foldedKPIs)
            }
        }

        // How far to trust it: yesterday's calls graded, and the summary's
        // check against the night's facts — a line and a chip, each opening
        // its detail (10/8/26).
        if let y = report.yesterday, !y.items.isEmpty {
            DSRYesterdayLine(yesterday: y)
        }
        if let v = report.narrative?.verification, !v.ownerLines.isEmpty {
            DSRVerificationChip(verification: v)
        }
        // A block this login's view leaves out is SAID, not silently
        // missing — "withheld" is not "absent" (D3-13).
        if let line = report.withheldLine {
            Text(line)
                .cavnarText(.caption, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }

        footer(report)
    }

    /// Top KPIs shown before "All KPIs" (10/8/26: four slim tiles).
    static let kpisShown = 4
    /// Priorities on screen before "All priorities": the first in "Do this
    /// today" and two in the list (density #2).
    static let prioritiesShown = 3

    /// The KPIs drawn, and how many more "All KPIs" would add: the top
    /// list's first `kpisShown`, or — opened — every KPI.
    static func kpiSplit(top: [DSRKPI], all: [DSRKPI], showingAll: Bool) -> (shown: [DSRKPI], hidden: Int) {
        guard !showingAll else { return (all, 0) }
        let shown = Array(top.prefix(kpisShown))
        return (shown, max(0, all.count - shown.count))
    }

    /// "Still missing" says how many parts aren't in yet — each block card
    /// carries its own reason (re-audit D5: the caveat repeated every one).
    /// A reason no block card shows stays in the caveat. Nil when nothing
    /// is missing.
    static func missingLine(_ report: DSRReport) -> String? {
        let missing = report.facts.missing
        guard !missing.isEmpty else { return nil }
        let onCards = Set(report.displayedBlocks
            .filter { !$0.block.isReady && $0.block != DSRBlock.notCollected }
            .compactMap { $0.block.reason })
        let carded = missing.filter { onCards.contains($0) }
        let loose = missing.filter { !onCards.contains($0) }
        var parts: [String] = []
        if !carded.isEmpty {
            let n = carded.count
            parts.append("\(n) part\(n == 1 ? "" : "s") of the night \(n == 1 ? "isn\u{2019}t" : "aren\u{2019}t") in yet \u{2014} each says why under The night in full.")
        }
        parts += loose
        return parts.joined(separator: " ")
    }

    /// Whether "The night in full" has anything to open.
    static func hasNightInFull(_ report: DSRReport, blocks: Int, hiddenKPIs: Int) -> Bool {
        blocks > 0 || hiddenKPIs > 0 || !report.insights.isEmpty || !report.notCollectedTitles.isEmpty
    }

    @ViewBuilder
    private func nightInFull(_ report: DSRReport, blocks: [(name: String, block: DSRBlock)],
                             hiddenKPIs: Int) -> some View {
        DSRInsightsGrid(insights: report.insights)
        if hiddenKPIs > 0 {
            disclosureButton(showingAllKPIs ? "Fewer numbers" : "All numbers (\(report.kpis.count))",
                             open: showingAllKPIs) { showingAllKPIs.toggle() }
            if showingAllKPIs {
                DSRKPIGrid(title: "All numbers", kpis: report.kpis)
            }
        }
        // Each measured block as a card; a block the night has no row for
        // is one caption line, not a card of its own (10/8/26).
        if !blocks.isEmpty {
            DSRSectionTitle(title: "The night, block by block")
            ForEach(Array(blocks.enumerated()), id: \.element.name) { index, entry in
                blockCard(entry.name, entry.block)
                    .cavnarRowEntrance(index: index, clock: clock)
            }
        }
        if !report.notCollectedTitles.isEmpty {
            HomeMixedText.make("Not collected this night: \(DSRText.list(report.notCollectedTitles))",
                               role: .caption, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func disclosureButton(_ label: String, open: Bool, _ toggle: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            withAnimation(.easeOut(duration: 0.2)) { toggle() }
        } label: {
            HStack(spacing: CavnarSpace.xxs + 2) {
                HomeMixedText.make(label, role: .label, color: .cavnarEmber2)
                Image(systemName: "chevron.down")
                    .font(.cavnar(.caption))
                    .rotationEffect(.degrees(open ? 180 : 0))
                    .accessibilityHidden(true)
                Spacer(minLength: 0)
            }
            .foregroundStyle(Color.cavnarEmber2)
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityValue(open ? "Expanded" : "Collapsed")
    }

    @ViewBuilder
    private func leadCard(_ report: DSRReport) -> some View {
        let kicker = report.isOwnerView ? "Executive summary" : "Operations summary"
        if let summary = report.narrative?.executiveSummary {
            DSRLeadCard(kicker: kicker, text: summary.text)
        } else if report.phase.isTerminal {
            Text(noSummaryLine(report))
                .cavnarText(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard()
        }
    }

    /// "Staffing for Friday: see priority 2 above" — Tomorrow points at the
    /// staffing action among the priorities, never says it a second time
    /// (10/8/26; the web's rule). Nil when no priority is about staffing.
    private func staffingPointer(_ report: DSRReport) -> String? {
        let words = ["staff", "schedul", "labor", "shift", "cover"]
        guard let actions = report.narrative?.actionsTomorrow,
              let i = actions.firstIndex(where: { a in
                  let s = ((a.kind ?? "") + " " + a.text).lowercased()
                  return words.contains { s.contains($0) }
              }) else { return nil }
        let day = report.tomorrow?.weekday ?? DSRFormat.weekday(report.businessDate, offset: 1)
        let target = i == 0 ? "the first priority above" : "priority \(i + 1) above"
        return "Staffing" + (day.map { " for \($0)" } ?? "") + ": see " + target
    }

    private func noSummaryLine(_ report: DSRReport) -> String {
        if let reason = report.checklist?.narrative?.reason, !reason.isEmpty {
            return "No written summary for this night — \(reason). Every figure below is still measured."
        }
        return "No written summary for this night. Every figure below is still measured."
    }

    /// The priorities after the first (which leads as "Do this today"):
    /// two on screen, collapsed to their line, the rest one tap away.
    @ViewBuilder
    private func priorities(_ n: DSRNarrative, report: DSRReport) -> some View {
        let rest = Array(n.actionsTomorrow.dropFirst())
        if !rest.isEmpty {
            let cap = Self.prioritiesShown - 1
            let shown = showingAllPriorities ? rest : Array(rest.prefix(cap))
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                Text(report.scorecard?.labels["priorities"]
                     ?? DSRFormat.weekday(report.businessDate, offset: 1).map { "\($0)'s priorities" }
                     ?? "Next priorities")
                    .cavnarText(.headline)
                    .accessibilityAddTraits(.isHeader)
                ForEach(Array(shown.enumerated()), id: \.element.id) { i, action in
                    DSRPriorityRow(number: i + 2, action: action)
                    if i < shown.count - 1 { AccountRowDivider() }
                }
                if rest.count > cap {
                    CavnarMoreToggle(hiddenCount: rest.count - cap, isExpanded: $showingAllPriorities)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    /// Whether the score card states the night's net (re-audit D6) — then
    /// the Sales block neither opens nor closes on it again.
    private var netOnScorecard: Bool {
        guard let report = viewModel.report, report.scorecard != nil,
              let sales = report.facts.blocks["sales"] else { return false }
        return sales.isReady && sales.metric("net") != nil
    }

    private func blockCard(_ name: String, _ block: DSRBlock) -> some View {
        let date = viewModel.report?.businessDate ?? viewModel.businessDate
        return DSRBlockCard(title: DSRBlock.titles[name] ?? name.capitalized, block: block,
                     headline: DSRHeadline.line(for: name, block, businessDate: date, netOnScorecard: netOnScorecard),
                     isExpanded: Binding(get: { expanded.contains(name) },
                                         set: { if $0 { expanded.insert(name) } else { expanded.remove(name) } })) {
            DSRBlockBody(name: name, block: block, businessDate: date,
                         blocks: viewModel.report?.facts.blocks ?? [:],
                         isOwner: viewModel.report?.isOwnerView == true,
                         netOnScorecard: netOnScorecard,
                         onMap: { mapping = $0 })
        }
    }

    @ViewBuilder
    private func footer(_ report: DSRReport) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            // A night that couldn't finish, or finished with data missing:
            // "Try again now" for any console login, as on the web (D3-9).
            if viewModel.phase == .failed || viewModel.phase == .provisional {
                closeDayControl
            }
            if let why = report.checklist?.rerun?.reason ?? viewModel.checklist?.rerun?.reason, !why.isEmpty {
                Text(why).cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Re-run and the week are the toolbar's ("…" and the grid icon)
            // — one place each (re-audit D18). A re-run's error still shows.
            if viewModel.canRerun, !showsProgress, !viewModel.canCloseDay, let error = viewModel.actionError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // The report's settings live on the web (re-audit D13).
            if viewModel.isOwner {
                CavnarWebLinkRow(title: "Report settings",
                                 subtitle: "When it\u{2019}s sent, the deadline, gross, your calendar and POS departments",
                                 path: "account/report")
            }
        }
        .padding(.top, 6)
    }
}

// MARK: - The night's lead

/// The executive summary (10/8/26, readability item 14): its first sentence
/// is the headline; the rest shows three lines with "Read more". Only the
/// opened text follows the phone's text size past the app's cap — the
/// collapsed card stays in proportion with the cards around it.
struct DSRLeadCard: View {
    let kicker: String
    let text: String
    @State private var expanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// Three body lines on a phone hold about this many characters.
    static let clampAfter = 140
    @State private var clampedHeight: CGFloat = 0
    @State private var fullHeight: CGFloat = 0

    /// "Read more" when the three-line clamp cut the text — measured, so a
    /// large Dynamic Type size that truncates a short summary still opens
    /// (re-audit D4) — or, before it is measured, past `clampAfter`.
    static func clamps(characters: Int, full: CGFloat, clamped: CGFloat) -> Bool {
        if full > 0, clamped > 0 { return full > clamped + 1 }
        return characters > clampAfter
    }

    var body: some View {
        let parts = DSRText.split(text)
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(kicker)
            CavnarMixedText(parts.first, role: .lead)
                .accessibilityAddTraits(.isHeader)
            if let rest = parts.rest {
                if expanded {
                    CavnarMixedText(rest, role: .body)
                        // The night in sentences, opened: read, not scanned,
                        // so it follows the phone's text size past the cap.
                        .cavnarReadingSize()
                        .transition(reduceMotion ? .identity : .opacity)
                } else {
                    HomeMixedText.make(rest, role: .body)
                        .cavnarText(.body)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { clampedHeight = $0 }
                        // The whole text, unclamped and unseen, at the same
                        // width: taller than the three lines means a large
                        // text size cut it, so "Read more" shows (re-audit
                        // D4) — not only past a character count.
                        .background(alignment: .top) {
                            HomeMixedText.make(rest, role: .body)
                                .cavnarText(.body)
                                .fixedSize(horizontal: false, vertical: true)
                                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { fullHeight = $0 }
                                .hidden()
                                .accessibilityHidden(true)
                        }
                }
                if expanded || Self.clamps(characters: rest.count, full: fullHeight, clamped: clampedHeight) {
                    Button {
                        Haptic.light()
                        if reduceMotion { expanded.toggle() } else {
                            withAnimation(.easeOut(duration: 0.2)) { expanded.toggle() }
                        }
                    } label: {
                        HStack(spacing: CavnarSpace.xxs + 2) {
                            Text(expanded ? "Show less" : "Read more")
                                .font(.cavnarBody(CavnarType.body, weight: 700))
                            Image(systemName: "chevron.down")
                                .font(.cavnar(.caption))
                                .rotationEffect(.degrees(expanded ? 180 : 0))
                                .accessibilityHidden(true)
                            Spacer(minLength: 0)
                        }
                        .foregroundStyle(Color.cavnarEmber2)
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.ai)
    }
}

// MARK: - Priorities

/// The urgency chip and the dollars, on one line under an action.
struct DSRActionChips: View {
    let action: DSRAction

    var body: some View {
        if action.urgencyLabel != nil || action.dollarsLine != nil {
            AccountFlowLayout(spacing: CavnarSpace.xs) {
                if let urgency = action.urgencyLabel {
                    // The urgency is a status: amber for today, ink
                    // otherwise — never the ember (B4 L7).
                    AccountChip(text: urgency, muted: true, tint: DailyReportView.urgencyTint(action.urgency))
                }
                // The dollars, calibrated by measured results when the
                // server corrected them (F6).
                if let dollars = action.dollarsLine {
                    HomeMixedText.make(dollars, role: .secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// What a priority holds behind its tap: what another module knows against
/// it, the urgency move, the effort, how sure, advice it pulls against, and
/// Done / Not for us / Track.
struct DSRActionDetail: View {
    let action: DSRAction
    /// The confidence line is drawn by the caller (the answer card shows
    /// it above the fold).
    var showsConfidence = true
    var showsAnswers = true

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            // What another module knows against it (a trim on a night
            // guests complained about service — M3's trim guard).
            if let caution = action.caution {
                RecCautionLine(text: caution)
            }
            // "Today" the cited facts didn't carry, moved (H13).
            if let moved = action.urgencyAdjustedLine {
                HomeMixedText.make(moved + ".", role: .caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let effort = action.effortLabel {
                Text(effort).cavnarText(.caption, color: .cavnarInk2)
            }
            // How sure, from the facts it cites (K1).
            if showsConfidence, let c = action.confidence {
                ConfidenceLine(confidence: c, recKey: action.answerKey, surface: "dsr", module: action.answerModule)
            }
            // Advice it pulls against, settled once (memory round 9/29/26,
            // lever_conflicts). The report is a stored record, so the
            // choice shapes the next one.
            if let conflict = action.conflict, action.answered != true {
                RecConflictPanel(conflict: conflict)
            }
            // Done / Not for us / Track, as on the web (rec-ROI #9). An
            // answered action keeps its line — the report is a record —
            // and drops the controls.
            if showsAnswers, action.showsAnswers, let key = action.answerKey {
                RecAnswerRow(key: key, surface: "dsr", module: action.answerModule)
                    .padding(.top, 2)
            }
        }
    }
}

/// The first priority, right under the score (10/8/26, readability item
/// 15): the action, its one-line why, urgency and dollars, how sure, and
/// the answer row; the rest of its reasoning behind "See the evidence".
struct DSRDoThisToday: View {
    let action: DSRAction

    /// "Do this today" only when it is for today — a this-week action is
    /// "Do this first", never called today's.
    static func kicker(_ a: DSRAction) -> String {
        switch a.urgency?.lowercased() {
        case "before_service", "today": return "Do this today"
        default: return "Do this first"
        }
    }

    private var whyRest: String? { action.why.flatMap { DSRText.split($0).rest } }

    private var hasDetail: Bool {
        whyRest != nil || action.caution != nil || action.urgencyAdjustedLine != nil
            || action.effortLabel != nil || (action.conflict != nil && action.answered != true)
    }

    private var confidence: ConfidenceLine? {
        action.confidence.map {
            ConfidenceLine(confidence: $0, recKey: action.answerKey, surface: "dsr", module: action.answerModule)
        }
    }

    var body: some View {
        if hasDetail {
            CavnarAnswerCard(kicker: Self.kicker(action), headline: action.text, summary: action.whyFirstSentence,
                             confidence: confidence) {
                actions
            } detail: {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    if let rest = whyRest {
                        CavnarMixedText(rest, role: .secondary)
                    }
                    DSRActionDetail(action: action, showsConfidence: false, showsAnswers: false)
                }
            }
        } else {
            CavnarAnswerCard(kicker: Self.kicker(action), headline: action.text, summary: action.whyFirstSentence,
                             confidence: confidence) {
                actions
            }
        }
    }

    @ViewBuilder
    private var actions: some View {
        DSRActionChips(action: action)
        if action.showsAnswers, let key = action.answerKey {
            RecAnswerRow(key: key, surface: "dsr", module: action.answerModule)
        }
    }
}

/// One of the remaining priorities (10/8/26, readability item 16):
/// collapsed it is the action, two lines of why, the urgency and the
/// dollars; a tap opens the rest — and the answer row, so Done / Not for
/// us / Track stay one tap away.
struct DSRPriorityRow: View {
    let number: Int
    let action: DSRAction
    @State private var open = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Button {
                Haptic.light()
                if reduceMotion { open.toggle() } else { withAnimation(.easeOut(duration: 0.22)) { open.toggle() } }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                    Text("\(number)")
                        .font(.cavnar(.figureS))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(width: 20, alignment: .leading)
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        CavnarMixedText(action.text, role: .label)
                        if let why = action.why {
                            HomeMixedText.make(why, role: .secondary)
                                .lineLimit(open ? nil : 2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        DSRActionChips(action: action)
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarInk2)
                        .rotationEffect(.degrees(open ? 180 : 0))
                        .accessibilityHidden(true)
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint(open ? "Hides the detail" : "Shows how sure, the effort and the answers")
            if open {
                DSRActionDetail(action: action)
                    .padding(.leading, 32)
                    .transition(reduceMotion ? .identity : .opacity)
            }
        }
    }
}

// MARK: - The check

/// "Checked against the night's facts" — a chip that opens what the check
/// kept and left out, in an owner's words (10/8/26: the footer sentence
/// read like a test log).
struct DSRVerificationChip: View {
    let verification: DSRVerification
    @State private var showing = false

    var body: some View {
        Button {
            Haptic.light()
            showing = true
        } label: {
            HStack(spacing: CavnarSpace.xs) {
                Image(systemName: "checkmark.seal.fill")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarGreen)
                    .accessibilityHidden(true)
                Text("Checked against the night\u{2019}s facts")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk2)
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, CavnarSpace.s)
            .padding(.vertical, CavnarSpace.xs)
            .background(Capsule().fill(Color.cavnarGreen.opacity(0.12)))
            .overlay(Capsule().strokeBorder(Color.cavnarGreen.opacity(0.3), lineWidth: 1))
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityHint("Shows how the written summary was checked")
        .sheet(isPresented: $showing) {
            NavigationStack {
                ScrollView {
                    VStack(alignment: .leading, spacing: CavnarSpace.m) {
                        Text("Before you see it, Cavnar AI checks every figure in the written summary against the night\u{2019}s own numbers. A line that doesn\u{2019}t match is left out.")
                            .cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                        VStack(alignment: .leading, spacing: CavnarSpace.s) {
                            ForEach(Array(verification.ownerLines.enumerated()), id: \.offset) { _, line in
                                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                                    Image(systemName: "checkmark")
                                        .font(.cavnar(.secondary))
                                        .foregroundStyle(Color.cavnarGreen)
                                        .accessibilityHidden(true)
                                    CavnarMixedText(line, role: .body)
                                }
                            }
                        }
                        .cavnarCard()
                    }
                    .padding(CavnarSpace.gutter)
                }
                .accountSheetChrome("How the summary was checked")
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
    }
}

// MARK: - Block bodies

/// What each block shows when opened — only the parts the payload carries.
struct DSRBlockBody: View {
    let name: String
    let block: DSRBlock
    var businessDate: String?
    /// Every block this login was sent — the night in detail reads the
    /// Sales and Labor blocks beside the Service one.
    var blocks: [String: DSRBlock] = [:]
    /// The owner places an unmapped POS department ("Map it").
    var isOwner = false
    /// The score card above states the net (re-audit D6): the opened Sales
    /// block doesn't repeat it as its figure.
    var netOnScorecard = false
    var onMap: ((DSRBlock.Unmapped) -> Void)? = nil

    /// The Service block draws the night's hour chart and what was given
    /// away when it is ready (re-audit D7, D8) — Sales and Labor then leave
    /// theirs to it.
    private var serviceReady: Bool { blocks["service"]?.isReady == true }
    private var serviceDrawsHours: Bool {
        serviceReady && DSRHourStory(sales: blocks["sales"], labor: blocks["labor"]) != nil
    }
    private var serviceDrawsGiven: Bool { serviceReady && blocks["service"]?.lossGiven != nil }

    var body: some View {
        switch name {
        case "sales": sales
        case "labor": labor
        case "service": DSRNightDetail(service: block, blocks: blocks, businessDate: businessDate)
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
                // The net is the score card's when it states it (re-audit
                // D6); a manager's view, with no score card, opens on it.
                if !netOnScorecard {
                    CavnarKicker("Net sales")
                    // FigureL: the score above is the screen's one FigureXL.
                    Text(DSRFormat.money(m("net")))
                        .cavnarText(.figureL, color: m("net") == nil ? Color.cavnarInk3 : Color.cavnarInk)
                        .cavnarSensitive()
                }
                if let line = joined([
                    // An "all" gross the POS couldn't complete says so
                    // here, never the items figure passed off as it.
                    // Gross isn't in a manager's payload at all: nothing
                    // is said about it then (neither a figure nor a gap).
                    m("gross").map { "Gross \(DSRFormat.money($0))" } ?? (block.has("gross") ? block.grossNotMeasured : nil),
                    m("transactions").map { "\(DSRFormat.count($0)) checks" },
                    m("guests").map { "\(DSRFormat.count($0)) guests" },
                    m("avg_ticket").map { "\(DSRFormat.money($0)) avg check" },
                ]) {
                    HomeMixedText.make(line, role: .secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // The night's target is the owner's nightly-sales goal when
                // no budget was entered — said as their goal (M5).
                if let goal = block.budgetGoalLabel {
                    HomeMixedText.make(goal, role: .secondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let budget = joined([
                    block.has("budget_net") ? "\(DSRFormat.money(m("budget_net"))) net" : nil,
                    block.has("budget_gross") ? "\(DSRFormat.money(m("budget_gross"))) gross" : nil,
                ]) {
                    HomeMixedText.make("Budget " + budget, role: .secondary)
                }
            }

            let comparisons = salesComparisons
            if !comparisons.isEmpty {
                VStack(spacing: 0) {
                    ForEach(Array(comparisons.enumerated()), id: \.offset) { i, c in
                        HStack {
                            Text(c.label).cavnarText(.body)
                            Spacer()
                            Text(DSRFormat.signedPct(c.pct))
                                .font(.cavnarNumber(CavnarType.body, weight: 700))
                                .foregroundStyle(DSRFormat.tone(c.pct))
                            Text(DSRFormat.money(c.base))
                                .font(.cavnarNumber(CavnarType.secondary))
                                .foregroundStyle(Color.cavnarInk2)
                                .fixedSize().frame(minWidth: 80, alignment: .trailing).layoutPriority(1)
                        }
                        .padding(.vertical, 8)
                        .accessibilityElement(children: .combine)
                        if i < comparisons.count - 1 { AccountRowDivider() }
                    }
                }
            }
            // What the forecast rested on, each measured effect named — or
            // why there is no forecast comparison tonight (M5).
            if let basis = block.forecastBasis {
                HomeMixedText.make("Forecast: " + basis, role: .caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let reason = block.forecastMissingReason {
                HomeMixedText.make("No forecast comparison: " + reason, role: .caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            let cats = block.categories
            if !cats.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    CavnarKicker("By category")
                    DSRCategoryBars(categories: cats)
                }
            }
            unmappedDepartments

            // The hour story in the night in detail carries the hours when
            // the Service block is ready (re-audit D7) — one chart, not two.
            let hours = block.hourly
            if !hours.isEmpty, !serviceDrawsHours {
                VStack(alignment: .leading, spacing: 10) {
                    CavnarKicker("By hour")
                    DSRHourlyBars(hours: hours)
                }
            }

            itemList("Top items", block.topItems)
            // The night's slowest sellers, as the web's "Slowest" table
            // (parity audit #33).
            itemList("Slowest", block.slowestItems)

            // Discounts always; comps, voids and refunds only when this
            // login may see them (the server drops them otherwise).
            // Tax last, captioned with where it sits for this restaurant's
            // gross (the web's tile says the same).
            // When the night in detail carries what was given away (re-audit
            // D8), the loss tiles are its, not repeated here; tax stays.
            let loss = [("Discounts", "discounts"), ("Comps", "comps"), ("Voids", "voids"), ("Refunds", "refunds")]
                .filter { !serviceDrawsGiven && block.has($0.1) }
                .map { DSRStatTile(label: $0.0, value: DSRFormat.money(m($0.1))) }
                + (m("tax").map { [DSRStatTile(label: "Tax collected", value: DSRFormat.money($0),
                                               detail: block.taxCaption)] } ?? [])
            if !loss.isEmpty { DSRTileRow(tiles: loss) }

            if let note = block.definitionNote {
                HomeMixedText.make(note, role: .caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder
    private func itemList(_ title: String, _ items: [DSRBlock.Item]) -> some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                CavnarKicker(title)
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline) {
                        Text(item.name).cavnarText(.body).lineLimit(1)
                        Spacer()
                        Text(DSRFormat.count(item.qty)).font(.cavnarNumber(CavnarType.secondary)).foregroundStyle(Color.cavnarInk2)
                        Text(DSRFormat.money(item.net)).font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk)
                            .fixedSize().frame(minWidth: 80, alignment: .trailing).layoutPriority(1)
                    }
                }
            }
        }
    }

    /// POS departments the night couldn't place — shown on their own, never
    /// guessed — each with "Map it" for the owner (parity audit #33; the
    /// web's "Map departments" / "Place it").
    @ViewBuilder
    private var unmappedDepartments: some View {
        let un = block.unmappedDepartments
        if !un.isEmpty || block.unallocated != nil {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(un) { u in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(u.department).cavnarText(.label)
                                Text(u.newIn == nil ? "Not in a category" : "New")
                                    .font(.cavnarBody(CavnarType.caption, weight: 700))
                                    .foregroundStyle(Color.cavnarAmber)
                            }
                            HomeMixedText.make(DSRCategoryMapSheet.why(u), role: .caption, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 8)
                        Text(DSRFormat.money(u.net)).font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk)
                        if isOwner, let onMap {
                            Button {
                                Haptic.light()
                                onMap(u)
                            } label: {
                                Text(u.newIn == nil ? "Map it" : "Place it").font(.cavnarBody(CavnarType.secondary, weight: 700))
                                    .foregroundStyle(Color.cavnarEmber2)
                                    .cavnarHitTarget()
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                if let gap = block.unallocated {
                    HomeMixedText.make("\(DSRFormat.money(gap)) of net isn\u{2019}t in any department on the POS.",
                                       role: .caption, color: .cavnarInk2)
                }
            }
        }
    }

    private var salesComparisons: [(label: String, pct: Double?, base: Double?)] {
        // The same words the collapsed Sales line uses (re-audit D19).
        let lastWeekLabel = DSRHeadline.lastWeekLabel(businessDate)
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
                Text(DSRFormat.pct(m("pct"))).cavnarText(.figureL, color: laborTone)
                if let line = joined([
                    "of net sales",
                    m("target_pct").map { "target \(DSRFormat.pct($0))" },
                    m("vs_target_pts").map { DSRFormat.signedPoints($0) },
                ]) {
                    HomeMixedText.make(line, role: .secondary)
                }
            }
            // The web's labor tiles, in its order and words (D3-13): After
            // 6pm, No-shows, Late clock-ins, Shift quality when measured.
            DSRTileRow(tiles: laborTiles)
            // Where the labor went: in the night in detail when the Service
            // block is ready, here otherwise (a POS without check detail).
            if blocks["service"]?.isReady != true, !block.departments.isEmpty {
                DSRLaborDepartments(labor: block, salesNet: blocks["sales"]?.metric("net"))
            }
            if !block.observations.isEmpty {
                DSRLineList(lines: block.observations.map { DSRLine(text: $0) }, dot: .cavnarInk3)
            }
            if let note = block.coverageNote {
                Text(note).cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var laborTiles: [DSRStatTile] {
        var tiles = [
            DSRStatTile(label: "Labor cost", value: DSRFormat.money(m("cost"))),
            DSRStatTile(label: "Hours", value: DSRFormat.count(m("hours"))),
            DSRStatTile(label: "Overtime", value: m("overtime_hours").map { "\(DSRFormat.count($0)) hrs" } ?? DSRFormat.dash),
        ]
        // Share of the night's hours worked after 6pm, against its share of
        // sales — only when both were measured.
        if let hours = m("hours_after_6pm_share_pct"), let sales = m("sales_after_6pm_share_pct") {
            tiles.append(DSRStatTile(label: "After 6pm", value: "\(DSRFormat.pct(hours)) of hours",
                                     detail: "against \(DSRFormat.pct(sales)) of sales"))
        }
        if let v = m("no_shows") { tiles.append(DSRStatTile(label: "No-shows", value: DSRFormat.count(v))) }
        if let v = m("late_arrivals") { tiles.append(DSRStatTile(label: "Late clock-ins", value: DSRFormat.count(v))) }
        if let v = m("shift_quality") {
            tiles.append(DSRStatTile(label: "Shift quality", value: DSRFormat.count(v)))
        }
        return tiles
    }

    private var foodTiles: [DSRStatTile] {
        var tiles = [
            DSRStatTile(label: "Est. food cost", value: DSRFormat.pct(m("est_food_cost_pct")),
                        detail: m("est_food_cost").map { DSRFormat.money($0) }),
            DSRStatTile(label: "Waste logged", value: DSRFormat.money(m("waste_logged")),
                        detail: m("waste_inferred").map { "\(DSRFormat.money($0)) inferred" }),
            DSRStatTile(label: "Variance", value: DSRFormat.money(m("variance_cost")),
                        detail: m("variance_items").map { "\(DSRFormat.count($0)) item\($0 == 1 ? "" : "s")" }),
        ]
        // Running low, as on the web (D3-13).
        if let low = m("critical_low") {
            tiles.append(DSRStatTile(label: "Running low", value: "\(DSRFormat.count(low)) critical",
                                     tone: low > 0 ? .cavnarRed : .cavnarInk,
                                     detail: m("low_stock").map { "\(DSRFormat.count($0)) more low" }))
        }
        return tiles
    }

    private var reviewTiles: [DSRStatTile] {
        var tiles = [
            DSRStatTile(label: "Received", value: DSRFormat.count(m("received"))),
            // No night rating under the floor (I11): the dash says why.
            DSRStatTile(label: "Average", value: DSRFormat.rating(m("avg_rating")),
                        detail: m("avg_rating") == nil ? block.ratingNote : nil),
            DSRStatTile(label: "Urgent", value: DSRFormat.count(m("urgent")),
                        tone: (m("urgent") ?? 0) > 0 ? .cavnarRed : .cavnarInk),
        ]
        // As on the web (D3-13), when measured.
        if let v = m("drafts_awaiting") { tiles.append(DSRStatTile(label: "Drafts waiting", value: DSRFormat.count(v))) }
        if let v = m("replies_posted") { tiles.append(DSRStatTile(label: "Replies posted", value: DSRFormat.count(v))) }
        return tiles
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
            DSRTileRow(tiles: foodTiles)
            // The % withheld under the coverage floor, and why (I11) — the
            // dash above is not a zero.
            if let note = block.foodCoverageNote {
                HomeMixedText.make(note, role: .caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // The cost drivers' monthly total is an opportunity, never money
            // recovered: it carries its basis, and "partial" when a source
            // was missing (NS1 #7). The renamed key is read first.
            if let line = atStakeLine {
                CavnarMixedText(line, role: .body)
            }
            let stock = block.criticalStock
            if !stock.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    CavnarKicker("Critically low")
                    ForEach(stock, id: \.self) { s in
                        HStack(alignment: .firstTextBaseline) {
                            Text(s.item).cavnarText(.body)
                            Spacer()
                            Text(s.daysRemaining.map { "\(DSRFormat.count($0)) days left" } ?? DSRFormat.dash)
                                .font(.cavnarNumber(CavnarType.secondary))
                                .foregroundStyle((s.daysRemaining ?? 1) < 1 ? Color.cavnarRedText : Color.cavnarAmber)
                        }
                    }
                    if let basis = block.stockBasis {
                        Text(basis).cavnarText(.caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            let variance = block.varianceItems
            if !variance.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    CavnarKicker("Usage over recipe")
                    ForEach(variance, id: \.self) { v in
                        HStack(alignment: .firstTextBaseline) {
                            Text(v.dish.map { "\(v.ingredient) · \($0)" } ?? v.ingredient)
                                .cavnarText(.body)
                            Spacer()
                            Text(DSRFormat.money(v.cost)).font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk)
                        }
                    }
                    if let window = block.varianceWindow {
                        HomeMixedText.make(window, role: .caption, color: .cavnarInk2)
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
            DSRTileRow(tiles: reviewTiles)
            let rows = block.reviewRows
            if !rows.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(rows.enumerated()), id: \.offset) { i, r in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(DSRFormat.rating(r.rating))
                                .font(.cavnarNumber(CavnarType.secondary, weight: 700))
                                .foregroundStyle((r.rating ?? 5) <= 2 ? Color.cavnarRedText : Color.cavnarInk2)
                                .frame(width: 44, alignment: .leading)
                            Text(r.summary).cavnarText(.body)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                            if r.urgent {
                                Text("Urgent").font(.cavnarBody(CavnarType.caption, weight: 700)).foregroundStyle(Color.cavnarRedText)
                            }
                        }
                        .padding(.vertical, 8)
                        if i < rows.count - 1 { AccountRowDivider() }
                    }
                }
            }
            if let note = block.syncNote {
                Text(note).cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
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
                    CavnarKicker("Posted")
                    ForEach(posts, id: \.self) { p in
                        HStack(alignment: .firstTextBaseline) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(p.topic).cavnarText(.body)
                                if let sub = joined([p.platform?.capitalized, p.at]) {
                                    HomeMixedText.make(sub, role: .caption, color: .cavnarInk2)
                                }
                            }
                            Spacer()
                            Text(p.reach.map { "\(DSRFormat.count($0)) reach" } ?? DSRFormat.dash)
                                .font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                        }
                    }
                }
            }
            let texts = block.textCampaigns
            if !texts.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    CavnarKicker("Texts")
                    ForEach(texts, id: \.self) { t in
                        HStack(alignment: .firstTextBaseline) {
                            CavnarMixedText(t.message, role: .body)
                            Spacer()
                            Text(t.sent.map { "\(DSRFormat.count($0)) sent" } ?? DSRFormat.dash)
                                .font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                        }
                    }
                }
            }
        }
    }

    // Intel — one line and the game card (10/8/26).

    /// "High 71° / low 58° · 20% rain · 84 covers booked · actual: light rain".
    private var intelLine: String? {
        joined([
            (m("weather_high_f") == nil && m("weather_low_f") == nil) ? nil
                : "High \(DSRFormat.degrees(m("weather_high_f"))) / low \(DSRFormat.degrees(m("weather_low_f")))",
            m("weather_precip_pct").map { "\(DSRFormat.pct($0)) rain" },
            m("reservations_covers").map { "\(DSRFormat.count($0)) covers booked" },
            block.weatherObservedSummary.map { "actual: \($0)" },
        ])
    }

    private var intel: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            if let line = intelLine {
                CavnarMixedText(line, role: .body)
            }
            // Tonight's game against the last one on the same side (parity
            // audit #33, Event Intelligence phase 2).
            if let game = block.detail["game"], game.object != nil {
                DSRGameCard(game: game)
            }
            if let note = joined([block.weatherNote, block.competitorsNote]) {
                Text(note).cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // Close-out — verbatim.

    private var closeout: some View {
        VStack(alignment: .leading, spacing: 0) {
            let fields = block.closeoutFields
            if fields.isEmpty {
                Text("Nothing was written.").cavnarText(.secondary)
            }
            ForEach(Array(fields.enumerated()), id: \.element.key) { i, f in
                VStack(alignment: .leading, spacing: 4) {
                    CavnarKicker(f.label)
                    Text(f.text)
                        .cavnarText(.body, color: .cavnarInk)
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
