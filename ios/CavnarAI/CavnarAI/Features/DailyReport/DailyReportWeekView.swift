import SwiftUI

/// Erik's weekly sheet as the app shows it: one row per day of the
/// restaurant's week (Wed → Tue at Simple EJ's), the categories, gross,
/// net, budget (owner only — a manager's payload has no budget columns),
/// last year, labor %, and the day's notes; then the week's totals and the
/// period to date. The day column stays put while the figures scroll
/// sideways. A day with no report is in the week with every measured
/// figure as a dash — never a zero.
///
/// Week | Period, as the web's Week and Period views: a period is the
/// fiscal period holding the date, one row per week, and a week's row
/// opens that week.
struct DailyReportWeekView: View {
    var open: (DailyReportRoute) -> Void
    @State private var date: String?
    @State private var kind: GridKind
    @State private var viewModel = DailyReportWeekViewModel()
    @State private var editingBudget = false
    /// Every column (categories, gross, budget, notes) instead of the four
    /// the phone shows by default.
    @State private var allColumns = false

    enum GridKind: String, CaseIterable, Identifiable {
        case week = "Week"
        case period = "Period"
        var id: String { rawValue }
    }

    init(date: String?, period: Bool = false, open: @escaping (DailyReportRoute) -> Void) {
        _date = State(initialValue: date)
        _kind = State(initialValue: period ? .period : .week)
        self.open = open
    }

    private var isPeriod: Bool { kind == .period }
    private var loadKey: String { kind.rawValue + (date ?? "") }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                CavnarSegmentedControl(selection: $kind, options: GridKind.allCases) { $0.rawValue }
                header
                if viewModel.isLoading && viewModel.grid == nil {
                    CavnarSkeletonLines(widths: [1, 1, 1, 1, 1, 0.8]).cavnarCard()
                } else if viewModel.needsCalendar {
                    // No fiscal calendar: the server's sentence, and where
                    // the calendar is set — never a dead end (parity #64).
                    VStack(alignment: .leading, spacing: 8) {
                        Text("No periods yet").cavnarText(.headline)
                        Text(viewModel.errorMessage ?? "Set your fiscal calendar to see periods.")
                            .cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("Set your calendar on the web: Account \u{2192} Daily report. Weeks read here in the meantime.")
                            .cavnarText(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("See the week") {
                            Haptic.light()
                            kind = .week
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    .cavnarCard()
                } else if let error = viewModel.errorMessage, viewModel.grid == nil {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(error).cavnarText(.body, color: .cavnarRedText)
                        Button("Try again") { Task { await viewModel.load(date: date, period: isPeriod) } }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    .cavnarCard()
                } else if let grid = viewModel.grid {
                    // Net, vs last year, vs budget and Labor % by default
                    // (10/8/26); every column one tap away.
                    let full = DSRWeekTable(grid: grid)
                    DSRWeekGrid(table: allColumns ? full : full.compact()) { day in
                        if grid.kind == "period" {
                            // A period's row is a week: open it here.
                            date = day
                            kind = .week
                        } else {
                            open(.report(date: day))
                        }
                    }
                        .opacity(viewModel.isLoading ? 0.6 : 1)
                        .animation(.easeOut(duration: 0.2), value: viewModel.isLoading)
                    if full.columns.count > full.compact().columns.count {
                        Button {
                            Haptic.light()
                            withAnimation(.easeOut(duration: 0.2)) { allColumns.toggle() }
                        } label: {
                            HStack(spacing: CavnarSpace.xxs + 2) {
                                Text(allColumns ? "Fewer columns" : "More columns")
                                    .font(.cavnarBody(CavnarType.body, weight: 700))
                                Image(systemName: allColumns ? "chevron.left" : "chevron.right")
                                    .font(.cavnar(.caption))
                                    .accessibilityHidden(true)
                                Spacer(minLength: 0)
                            }
                            .foregroundStyle(Color.cavnarEmber2)
                            .cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint(allColumns ? "Shows net, last year, budget and labor only"
                                                      : "Shows every category, gross and the notes")
                    }
                    if !grid.showsBudget {
                        Text("Budget columns are the owner\u{2019}s.")
                            .cavnarText(.caption, color: .cavnarInk2)
                    }
                    if viewModel.canEditBudget, grid.kind != "period", !grid.days.isEmpty,
                       grid.days.allSatisfy({ $0.lastYearNet == nil }) {
                        // The import stays on the web (re-audit 10/8/26 #10):
                        // say where, rather than leave the column empty.
                        Text("No last year here yet. Import last year\u{2019}s nights from your old workbooks on the web: Daily report \u{2192} The week \u{2192} Import last year.")
                            .cavnarText(.caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    weekActions(grid)
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 8)
            .padding(.bottom, 80)
        }
        .cavnarEmberRefreshable { await viewModel.load(date: date, period: isPeriod) }
        .cavnarModuleBackground()
        .navigationTitle(isPeriod ? "The period" : "The week")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar(isPeriod ? "The period" : "The week") }
        .cavnarEmberBackButton()
        .task(id: loadKey) { await viewModel.load(date: date, period: isPeriod) }
        .sheet(isPresented: $editingBudget) {
            if let grid = viewModel.grid {
                DSRBudgetSheet(days: grid.days) {
                    Task { await viewModel.load(date: date, period: isPeriod) }
                }
            }
        }
    }

    /// The week's own actions: the budget (owner) and the week as the
    /// server's .xlsx, shared (parity audit #63, #79).
    @ViewBuilder
    private func weekActions(_ grid: DSRGrid) -> some View {
        if grid.kind != "period" {
            HStack(spacing: 10) {
                if viewModel.canEditBudget {
                    Button {
                        Haptic.light()
                        editingBudget = true
                    } label: {
                        Label("Budget", systemImage: "dollarsign.circle").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                ShareLink(item: DSRWeekWorkbook(date: grid.start, filename: DSRWeekWorkbook.filename(start: grid.start)),
                          preview: SharePreview("Daily sales \u{00B7} \(CavnarDate.mdyRange(grid.start, grid.end))")) {
                    Label("Export to Excel", systemImage: "square.and.arrow.up").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            stepButton(-1, systemImage: "chevron.left", label: isPeriod ? "Previous period" : "Previous week")
            VStack(spacing: 3) {
                HomeMixedText.make(viewModel.grid?.label ?? kind.rawValue, role: .label)
                    .multilineTextAlignment(.center)
                if let g = viewModel.grid {
                    HomeMixedText.make(CavnarDate.mdyRange(g.start, g.end), role: .caption, color: .cavnarInk2)
                }
            }
            .frame(maxWidth: .infinity)
            stepButton(1, systemImage: "chevron.right", label: isPeriod ? "Next period" : "Next week")
        }
    }

    private func stepButton(_ step: Int, systemImage: String, label: String) -> some View {
        Button {
            Haptic.selection()
            if let next = viewModel.neighbour(step) { date = next }
        } label: {
            Image(systemName: systemImage)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
                .cavnarToolbarIconGlass()
        }
        .buttonStyle(.plain)
        .disabled(viewModel.grid == nil || viewModel.isLoading)
        .accessibilityLabel(label)
    }
}

// MARK: - The table, as plain values

/// The grid's columns and rows as strings, built once from the payload —
/// so what each cell says (a dash for a null, no budget for a manager) is
/// tested without a view.
struct DSRWeekTable {
    struct Column: Hashable {
        let title: String
        let width: CGFloat
        var numeric = true
    }

    enum Tone { case plain, muted, good, bad }

    struct Cell: Hashable {
        let text: String
        var tone: Tone = .plain
    }

    struct Row: Identifiable {
        let id: String
        let label: String
        /// The night this row opens; nil for totals and for a day with no report.
        let opens: String?
        let provisional: Bool
        let isTotal: Bool
        let cells: [Cell]
    }

    let columns: [Column]
    let rows: [Row]

    /// The phone's default columns (10/8/26), in this order — each only when
    /// the full table has it (a manager has no budget, a login without
    /// labor no Labor %).
    static let compactTitles = ["Net", "vs last yr", "vs budget", "Labor %"]

    /// The table cut to `compactTitles`, narrower so a phone shows it whole;
    /// every row keeps one cell per column.
    func compact() -> DSRWeekTable {
        let picks: [Int] = Self.compactTitles.compactMap { t in columns.firstIndex { $0.title == t } }
        let narrow: [String: CGFloat] = ["Net": 84, "vs last yr": 72, "vs budget": 74, "Labor %": 64]
        let cols = picks.map { i in Column(title: columns[i].title, width: narrow[columns[i].title] ?? columns[i].width,
                                           numeric: columns[i].numeric) }
        let rows = rows.map { r in
            Row(id: r.id, label: r.label, opens: r.opens, provisional: r.provisional, isTotal: r.isTotal,
                cells: picks.map { r.cells[$0] })
        }
        return DSRWeekTable(columns: cols, rows: rows)
    }

    private init(columns: [Column], rows: [Row]) {
        self.columns = columns
        self.rows = rows
    }

    init(grid: DSRGrid) {
        let budget = grid.showsBudget
        // A login without labor sees no Labor % column at all — the server
        // strips the values (redact_grid), and a column of dashes read as
        // "not measured" (D3-13).
        let labor = grid.showsLabor
        var cols = grid.categories.map { Column(title: $0, width: 82) }
        // Gross is withheld from a manager (DB, 9/25/26): no column of dashes.
        let gross = grid.showsGross
        if gross { cols += [Column(title: "Gross", width: 86)] }
        cols += [Column(title: "Net", width: 86)]
        // The web's three budget columns (D3-13): gross, net, and net against it.
        if budget {
            cols += [Column(title: "Budget gross", width: 96), Column(title: "Budget net", width: 90),
                     Column(title: "vs budget", width: 78)]
        }
        cols += [Column(title: "Last year", width: 86), Column(title: "vs last yr", width: 78)]
        if labor { cols += [Column(title: "Labor %", width: 68)] }
        cols += [Column(title: "Notes", width: 230, numeric: false)]
        columns = cols

        func money(_ v: Double?) -> Cell { Cell(text: DSRFormat.money(v), tone: v == nil ? .muted : .plain) }
        func change(_ v: Double?) -> Cell {
            guard let v else { return Cell(text: DSRFormat.dash, tone: .muted) }
            return Cell(text: DSRFormat.signedPct(v), tone: v > 0 ? .good : (v < 0 ? .bad : .plain))
        }
        func pct(_ v: Double?) -> Cell { Cell(text: DSRFormat.pct(v), tone: v == nil ? .muted : .plain) }
        func note(_ s: String?) -> Cell { Cell(text: s ?? "", tone: .muted) }

        // A period (rollup.period): one row per week, each opening that week.
        let weekRows: [Row] = grid.weeks.compactMap { w in
            guard let t = w.totals else { return nil }
            var cells = grid.categories.map { money(t.category($0)) }
            if gross { cells += [money(t.gross)] }
            cells += [money(t.net)]
            if budget { cells += [money(t.budgetGross), money(t.budgetNet), change(t.vsBudgetNetPct)] }
            cells += [money(t.lastYearNet), change(t.vsLastYearNetPct)]
            if labor { cells += [pct(t.laborPct)] }
            let measured = t.daysMeasured ?? 0
            cells += [note(measured >= 7 ? CavnarDate.mdyRange(w.start, w.end)
                           : "\(measured) of 7 days measured")]
            return Row(id: "wk-" + w.start, label: w.label ?? CavnarDate.mdyRange(w.start, w.end), opens: w.start,
                       provisional: false, isTotal: false, cells: cells)
        }

        var out: [Row] = weekRows + grid.days.map { d in
            var cells = grid.categories.map { money(d.category($0)) }
            if gross { cells += [money(d.gross)] }
            cells += [money(d.net)]
            if budget { cells += [money(d.budgetGross), money(d.budgetNet), change(d.vsBudgetNetPct)] }
            cells += [money(d.lastYearNet), change(d.vsLastYearNetPct)]
            if labor { cells += [pct(d.laborPct)] }
            cells += [note(d.notes)]
            return Row(id: d.date, label: d.displayLabel, opens: d.status == nil ? nil : d.date,
                       provisional: d.provisional == true || d.status == "provisional", isTotal: false, cells: cells)
        }

        func totalRow(_ id: String, _ label: String, _ t: DSRGridTotals, note text: String?) -> Row {
            var cells = grid.categories.map { money(t.category($0)) }
            if gross { cells += [money(t.gross)] }
            cells += [money(t.net)]
            if budget { cells += [money(t.budgetGross), money(t.budgetNet), change(t.vsBudgetNetPct)] }
            cells += [money(t.lastYearNet), change(t.vsLastYearNetPct)]
            if labor { cells += [pct(t.laborPct)] }
            cells += [note(text)]
            return Row(id: id, label: label, opens: nil, provisional: false, isTotal: true, cells: cells)
        }

        if let t = grid.totals {
            let measured = t.daysMeasured ?? 0
            if grid.kind == "period" {
                out.append(totalRow("totals", "Period", t, note: CavnarDate.mdyRange(grid.start, grid.end)
                                    + " · \(measured) day\(measured == 1 ? "" : "s") measured"))
            } else {
                let text = grid.days.isEmpty || measured >= grid.days.count ? nil
                    : "\(measured) of \(grid.days.count) days measured"
                out.append(totalRow("totals", "Week", t, note: text))
            }
        }
        if let p = grid.periodToDate {
            let range = p.start.flatMap { s in p.end.map { CavnarDate.mdyRange(s, $0) } }
            out.append(totalRow("ptd", "Period to date", p, note: range))
        }
        rows = out
    }
}

// MARK: - The grid

/// A table on iOS — the one place the app has one, because the owner's own
/// weekly sheet is a grid and reading it any other way loses the columns
/// he compares down. Pinned first column, horizontally scrolling figures,
/// numbers right-aligned in the number face (DESIGN_SYSTEM.md §8).
struct DSRWeekGrid: View {
    let table: DSRWeekTable
    var onOpen: (String) -> Void

    private let rowHeight: CGFloat = 46
    private let headerHeight: CGFloat = 32
    private let labelWidth: CGFloat = 116

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            VStack(spacing: 0) {
                headerCell("Day", width: labelWidth, numeric: false)
                ForEach(table.rows) { row in
                    labelCell(row)
                }
            }
            .background(Color.cavnarPaper2.opacity(0.95))
            .overlay(alignment: .trailing) {
                Rectangle().fill(Color.cavnarPaper3).frame(width: 1)
            }
            .zIndex(1)

            ScrollView(.horizontal, showsIndicators: true) {
                VStack(spacing: 0) {
                    HStack(spacing: 0) {
                        ForEach(table.columns, id: \.self) { c in headerCell(c.title, width: c.width, numeric: c.numeric) }
                    }
                    ForEach(table.rows) { row in
                        HStack(spacing: 0) {
                            ForEach(Array(zip(table.columns, row.cells).enumerated()), id: \.offset) { _, pair in
                                valueCell(pair.1, column: pair.0, isTotal: row.isTotal)
                            }
                        }
                        .frame(height: rowHeight)
                        .background(row.isTotal ? Color.cavnarEmber.opacity(0.06) : Color.clear)
                        .overlay(alignment: .top) { divider(row) }
                        // The whole row opens the night (10/8/26), not only
                        // its pinned label; VoiceOver uses the label's button.
                        .contentShape(Rectangle())
                        .onTapGesture {
                            guard let date = row.opens else { return }
                            Haptic.light()
                            onOpen(date)
                        }
                        .accessibilityHidden(row.opens != nil)
                    }
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        .background(Color.cavnarPaper2.opacity(0.6), in: RoundedRectangle(cornerRadius: CavnarRadius.card))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1))
    }

    private func divider(_ row: DSRWeekTable.Row) -> some View {
        Rectangle()
            .fill(row.isTotal && row.id == "totals" ? Color.cavnarPaper3 : Color.cavnarPaper3.opacity(0.5))
            .frame(height: row.isTotal && row.id == "totals" ? 1.5 : 1)
    }

    private func headerCell(_ title: String, width: CGFloat, numeric: Bool) -> some View {
        Text(title)
            .font(.cavnarBody(CavnarType.caption, weight: 700))
            .foregroundStyle(Color.cavnarInk2)
            .lineLimit(1)
            .minimumScaleFactor(0.85)
            .padding(.horizontal, 10)
            .frame(width: width, height: headerHeight, alignment: numeric ? .trailing : .leading)
    }

    @ViewBuilder
    private func labelCell(_ row: DSRWeekTable.Row) -> some View {
        let content = HStack(spacing: 6) {
            if row.provisional {
                Circle().fill(Color.cavnarAmber).frame(width: 6, height: 6)
                    .accessibilityLabel("Missing data")
            }
            HomeMixedText.make(row.label, size: CavnarType.caption, weight: 700,
                               color: row.opens == nil && !row.isTotal ? .cavnarInk2 : .cavnarInk)
                .lineLimit(2)
                .minimumScaleFactor(0.85)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .frame(width: labelWidth, height: rowHeight)
        .background(row.isTotal ? Color.cavnarEmber.opacity(0.06) : Color.clear)
        .overlay(alignment: .top) { divider(row) }

        if let date = row.opens {
            Button {
                Haptic.light()
                onOpen(date)
            } label: { content.contentShape(Rectangle()) }
            .buttonStyle(.plain)
            .accessibilityHint("Opens that night's report")
        } else {
            content
        }
    }

    private func valueCell(_ cell: DSRWeekTable.Cell, column: DSRWeekTable.Column, isTotal: Bool) -> some View {
        Text(cell.text)
            .font(column.numeric ? .cavnarNumber(CavnarType.caption, weight: isTotal ? 700 : 500) : .cavnarBody(CavnarType.caption))
            .foregroundStyle(color(cell.tone))
            .lineLimit(column.numeric ? 1 : 2)
            .minimumScaleFactor(column.numeric ? 0.85 : 1)
            .padding(.horizontal, 10)
            .frame(width: column.width, height: rowHeight, alignment: column.numeric ? .trailing : .leading)
    }

    private func color(_ tone: DSRWeekTable.Tone) -> Color {
        switch tone {
        case .plain: return .cavnarInk
        case .muted: return .cavnarInk2
        case .good: return .cavnarGreen
        case .bad: return .cavnarRedText
        }
    }
}
