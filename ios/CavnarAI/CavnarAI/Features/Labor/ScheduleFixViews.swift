import SwiftUI

// The schedule fix round's screens on the phone (schedule audit 10/3/26,
// UI wave I1): what the generate screen says before a draft, what the draft
// says it could not do, the managers' plan on the days and rows, the
// review's new parts, the one-tap "why" after a save, the redo sheet and
// the publish check's notes. Every line is the server's own or built from
// its fields; nothing here is a model's sentence.

// MARK: - A notice box (the review's amber / red / ink boxes, one shape)

struct ScheduleNotice<Actions: View>: View {
    let text: String
    var tone: Color = .cavnarAmber
    var symbol: String = "exclamationmark.triangle.fill"
    var detail: [String] = []
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: symbol)
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(tone)
                    .padding(.top, 3)
                VStack(alignment: .leading, spacing: 4) {
                    HomeMixedText.make(text, size: CavnarType.secondary, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    ForEach(detail, id: \.self) { line in
                        HomeMixedText.make(line, size: CavnarType.caption, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            actions()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(tone.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(tone.opacity(0.3), lineWidth: 1))
    }
}

extension ScheduleNotice where Actions == EmptyView {
    init(text: String, tone: Color = .cavnarAmber, symbol: String = "exclamationmark.triangle.fill",
         detail: [String] = []) {
        self.init(text: text, tone: tone, symbol: symbol, detail: detail) { EmptyView() }
    }
}

/// A small uppercase tag on a row or a day header — the house CHANGED /
/// REVIEW capsule, in a tone.
struct ScheduleRowTag: View {
    let text: String
    var tone: Color = .cavnarInk3
    var symbol: String? = nil

    var body: some View {
        HStack(spacing: 3) {
            if let symbol { Image(systemName: symbol).font(.cavnar(.tag)) }
            Text(text.uppercased())
                .font(.cavnarBody(CavnarType.tag, weight: 700))
                .tracking(0.5)
        }
        .foregroundStyle(tone)
        .padding(.horizontal, 5)
        .padding(.vertical, 1)
        .background(Capsule().fill(tone.opacity(0.15)))
    }
}

// MARK: - Generate screen: freshness, the budget caveat, the owner's words

/// Under the week picker: how current the sales behind the picked week
/// are (`demand_data_through.line`), why Generate refuses a week that is too
/// stale (`blocked` + `message`), the budget's caveat, and the one-line
/// "Anything for this week?" sent as `instruction`.
struct GenerateWeekNotes: View {
    @Bindable var viewModel: LaborViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let through = viewModel.generateForecast?.dataThrough {
                if through.blocked {
                    ScheduleNotice(text: through.message ?? "Your newest sales are too old to plan this week by.",
                                   tone: .cavnarRed, symbol: "hand.raised.fill",
                                   detail: ["Generate is off for this week until newer sales arrive \u{2014} sync your point of sale, then try again."])
                } else if let line = through.line {
                    HomeMixedText.make(line, size: CavnarType.caption, weight: 600, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let caveat = viewModel.generateForecast?.budgetBasis?.caveat {
                HomeMixedText.make(caveat, size: CavnarType.caption, weight: 600, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            TextField("Anything for this week? (optional)", text: $viewModel.generateInstruction, axis: .vertical)
                .font(.cavnar(.secondary))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(1...3)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                    .fill(Color.cavnarPaper2))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                    .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .disabled(viewModel.isGeneratingSchedule)
                .onChange(of: viewModel.generateInstruction) { _, text in
                    if text.count > 500 { viewModel.generateInstruction = String(text.prefix(500)) }
                }
                .accessibilityLabel("Anything for this week")
        }
        // The picked week's forecast is read by the Labor hero
        // (`.task(id: viewModel.generateWeek)`), which stays up under this
        // sheet — its button names the week and refuses a stale one.
    }
}

// MARK: - The draft's banners

/// What the generation could not do, said at the top of the draft: days it
/// could not write (with Redo these days), days nobody can work, a first
/// week with no history, a manager plan that failed, the week's manager
/// shortfall, how current the sales were.
struct DraftNotices: View {
    @Bindable var viewModel: LaborViewModel
    let result: GeneratedSchedule
    /// Which notices (iOS readability round, 10/8/26): `.urgent` — what
    /// could not be written or staffed, in red/amber, leads the draft;
    /// `.info` — a starting point, how current the sales were — sits under
    /// the draft's Details; `.all` both.
    enum Part { case all, urgent, info }
    var part: Part = .all
    var onOpenAvailability: () -> Void = {}
    var onOpenClosures: () -> Void = {}

    private var startingLine: String? {
        (result.review?.lines ?? []).first { $0.hasPrefix("A starting point") }
    }

    private var urgent: Bool { part != .info }
    private var info: Bool { part != .urgent }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if info, result.startingPoint?.noHistory == true {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    ScheduleRowTag(text: "Starting point", tone: .cavnarInk2, symbol: "flag.fill")
                    CavnarMixedText(startingLine ?? "A first draft with no shift history of its own \u{2014} drafted from your team, your floors and similar restaurants.",
                                    role: .secondary)
                }
            }
            if urgent, let line = result.partialLine {
                ScheduleNotice(text: line) {
                    Button {
                        Haptic.medium()
                        viewModel.openRedo(dates: result.unwritten.map(\.date))
                    } label: {
                        Label("Redo these days", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(viewModel.isGeneratingSchedule || result.historyId == nil)
                }
            }
            if urgent, let line = result.unstaffableLine {
                ScheduleNotice(text: line, detail: result.unstaffable.compactMap { d in
                    d.reasons.isEmpty ? nil
                        : CavnarDate.dayDate(d.day, d.date) + ": " + d.reasons.joined(separator: "; ")
                }) {
                    HStack(spacing: 16) {
                        Button("Availability") { onOpenAvailability() }
                        Button("Closures") { onOpenClosures() }
                    }
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarEmber2)
                    .buttonStyle(.plain)
                    .frame(minHeight: 44)
                }
            }
            if urgent, result.plan?.failed == true {
                ScheduleNotice(text: "Cavnar AI couldn\u{2019}t plan the managers\u{2019} shifts before writing this draft \u{2014} check every day has a manager on from open to close.",
                               tone: .cavnarRed)
            }
            if urgent, let text = result.coverage?.shortfall?.text {
                ScheduleNotice(text: text, tone: .cavnarRed, symbol: "person.crop.circle.badge.exclamationmark")
            }
            if info, let through = result.demandDataThrough, let line = through.line {
                CavnarMixedText(line, role: .caption,
                                color: through.stale == true || through.blind == true ? .cavnarAmber : .cavnarInk2)
            }
        }
    }
}

// MARK: - The owner's question about the managers' days (M-1)

/// The question as ONE row in the draft (iOS readability round, 10/8/26):
/// "Set 4 managers' usual days ›", opening the per-name editor as a sheet —
/// the inline multi-picker pushed the week several screens down.
struct ManagerQuestionRow: View {
    @Bindable var viewModel: LaborViewModel
    let question: String
    let names: [String]
    @State private var open = false

    var body: some View {
        Button {
            Haptic.light()
            open = true
        } label: {
            HStack(spacing: CavnarSpace.s) {
                Image(systemName: "person.badge.clock")
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    CavnarMixedText(names.count == 1 ? "Set \(names[0])\u{2019}s usual days"
                                                     : "Set \(names.count) managers\u{2019} usual days",
                                    role: .label)
                    Text("This week is an even split until you do.")
                        .cavnarText(.secondary)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .cavnarCard(.ai)
        .sheet(isPresented: $open) {
            NavigationStack {
                ScrollView {
                    ManagerQuestionCard(viewModel: viewModel, question: question, names: names)
                        .padding(CavnarSpace.gutter)
                }
                .accountSheetChrome("The managers\u{2019} days")
            }
            .presentationDetents([.large])
            .presentationDragIndicator(.visible)
        }
    }
}

/// "Which days and hours do Erik, Jim, Anthony and Andrew work?" — shown
/// while the plan asks it. Per name, the days they always work (weekday,
/// start, end), saved as their standing shifts; every draft then keeps them.
struct ManagerQuestionCard: View {
    @Bindable var viewModel: LaborViewModel
    let question: String
    let names: [String]

    @State private var drafts: [String: [LaborViewModel.StandingDraft]] = [:]
    @State private var open: String?

    private static let days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    private static let times: [String] = stride(from: 5 * 60, through: 26 * 60, by: 30).map {
        LaborViewModel.shiftTimeText(minutes: $0)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(question)
                .cavnarText(.headline)
                .fixedSize(horizontal: false, vertical: true)
            Text("Cavnar AI plans managers first, so every minute on the floor has one. With no usual days on file, this week is an even split \u{2014} set their real days and every draft keeps them.")
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(names, id: \.self) { name in
                personBlock(name)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.ai)
        .animation(.easeOut(duration: 0.2), value: open)
    }

    private func personBlock(_ name: String) -> some View {
        let rows = drafts[name] ?? []
        let saved = viewModel.managerNotes[name]
        return VStack(alignment: .leading, spacing: 8) {
            Button {
                Haptic.selection()
                open = open == name ? nil : name
                if drafts[name] == nil { drafts[name] = [LaborViewModel.StandingDraft()] }
            } label: {
                HStack {
                    Text(name).font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
                    Spacer()
                    Text(open == name ? "Close" : "Set their days")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if let saved {
                Text(saved)
                    .font(.cavnarBody(CavnarType.caption, weight: 600))
                    .foregroundStyle(saved.hasPrefix("Saved") ? Color.cavnarGreen : Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if open == name {
                ForEach(rows) { row in
                    rowEditor(name, row)
                }
                HStack(spacing: 14) {
                    Button {
                        drafts[name, default: []].append(LaborViewModel.StandingDraft())
                    } label: {
                        Label("Add a day", systemImage: "plus")
                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                    .frame(minHeight: 44)
                    Spacer()
                    Button {
                        Task {
                            if await viewModel.saveStandingShifts(name: name, rows: drafts[name] ?? []) { open = nil }
                        }
                    } label: {
                        Group {
                            if viewModel.managerBusy == name { CavnarShimmerText(text: "Saving") } else { Text("Save their days") }
                        }
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: rows.isEmpty))
                    .disabled(rows.isEmpty || viewModel.managerBusy != nil)
                }
            }
            AccountRowDivider()
        }
    }

    private func rowEditor(_ name: String, _ row: LaborViewModel.StandingDraft) -> some View {
        HStack(spacing: 6) {
            picker(row.day, options: Self.days) { v in update(name, row.id) { $0.day = v } }
            picker(row.start, options: Self.times) { v in update(name, row.id) { $0.start = v } }
            Text("\u{2013}").foregroundStyle(Color.cavnarInk3)
            picker(row.end, options: Self.times) { v in update(name, row.id) { $0.end = v } }
            Spacer(minLength: 0)
            Button {
                drafts[name]?.removeAll { $0.id == row.id }
            } label: {
                Image(systemName: "xmark").font(.system(size: 11, weight: .bold)).foregroundStyle(Color.cavnarInk3)
                    .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Remove this day")
        }
    }

    private func update(_ name: String, _ id: UUID, _ apply: (inout LaborViewModel.StandingDraft) -> Void) {
        guard var rows = drafts[name], let i = rows.firstIndex(where: { $0.id == id }) else { return }
        apply(&rows[i])
        drafts[name] = rows
    }

    private func picker(_ value: String, options: [String], onPick: @escaping (String) -> Void) -> some View {
        Menu {
            ForEach(options, id: \.self) { o in Button(o) { onPick(o) } }
        } label: {
            HStack(spacing: 3) {
                Text(options.first == "Monday" ? String(value.prefix(3)) : value)
                    .font(.cavnarNumber(CavnarType.caption, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Image(systemName: "chevron.down").font(.system(size: 8, weight: .bold)).foregroundStyle(Color.cavnarEmber2)
            }
            .padding(.horizontal, 8)
            .frame(minHeight: 44)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper3.opacity(0.5)))
        }
    }
}

// MARK: - A day's header: the manager window, breaches on the day, gaps

/// Under a day's header in the table: "Manager on 11:00am–11:00pm", the
/// day-level breaches (never on an innocent person's row, E-13), each
/// stretch with no manager with why and what to do, and a standing shift
/// the plan could not use.
struct DayManagerNotes: View {
    @Bindable var viewModel: LaborViewModel
    let date: String
    let result: GeneratedSchedule
    var onChangeAvailability: () -> Void = {}

    private var hardDays: [ReviewHardDay] {
        (result.review?.hardDays?.items ?? []).filter { ($0.date ?? "").prefix(10) == date.prefix(10) }
    }
    private var left: [ManagerCoverage.Left] {
        (result.coverage?.left ?? []).filter { ($0.date ?? "").prefix(10) == date.prefix(10) }
    }
    private var planGaps: [ManagerPlan.Uncovered] {
        // The backstop's own stretches speak when it ran; the plan's are
        // the fallback (a reopened week keeps only the plan's).
        guard left.isEmpty else { return [] }
        return (result.plan?.uncovered ?? []).filter { ($0.date ?? "").prefix(10) == date.prefix(10) }
    }
    private var skipped: [ManagerPlan.Skipped] {
        (result.plan?.skipped ?? []).filter { ($0.date ?? "").prefix(10) == date.prefix(10) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let window = result.plan?.windowLine(for: date) {
                HStack(spacing: 5) {
                    Image(systemName: "person.badge.shield.checkmark")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Color.cavnarEmber2)
                    HomeMixedText.make(window, size: CavnarType.caption, weight: 600, color: .cavnarInk2)
                }
            }
            ForEach(Array(hardDays.enumerated()), id: \.offset) { _, breach in
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Color.cavnarRed)
                        .padding(.top, 3)
                    HomeMixedText.make(breach.detail ?? "A rule this week breaks on this day",
                                       size: CavnarType.caption, weight: 600, color: .cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(Array(left.enumerated()), id: \.offset) { _, gap in
                gapBox(from: gap.from, to: gap.to, reasons: gap.reasons.map(\.line), couldAct: gap.couldAct)
            }
            ForEach(Array(planGaps.enumerated()), id: \.offset) { _, gap in
                gapBox(from: gap.from, to: gap.to, reasons: gap.why.map { [$0] } ?? [], couldAct: [])
            }
            ForEach(Array(skipped.enumerated()), id: \.offset) { _, s in
                HomeMixedText.make(s.line, size: CavnarType.caption, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func gapBox(from: String?, to: String?, reasons: [String], couldAct: [String]) -> some View {
        let day = LaborViewModel.weekdayName(date)
        let span = [from, to].compactMap { $0 }.joined(separator: " to ")
        return ScheduleNotice(text: "No manager can be on \(CavnarDate.dayDate(day, date))\(span.isEmpty ? "" : " from \(span)")",
                              tone: .cavnarRed, symbol: "person.crop.circle.badge.exclamationmark",
                              detail: reasons) {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(couldAct, id: \.self) { name in
                    let key = name + "|" + date
                    if let note = viewModel.managerNotes[key] {
                        HomeMixedText.make(note, size: CavnarType.caption, weight: 600,
                                           color: note.contains("acting manager on") ? .cavnarGreen : .cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        Button {
                            Task { await viewModel.makeActingManager(name, on: date) }
                        } label: {
                            Group {
                                if viewModel.managerBusy == key {
                                    CavnarShimmerText(text: "Saving", color: .cavnarEmber2)
                                } else {
                                    Text("Make \(name) acting manager that day")
                                }
                            }
                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(minHeight: 44, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                        .disabled(viewModel.managerBusy != nil)
                    }
                }
                Button {
                    onChangeAvailability()
                } label: {
                    Text("Change availability")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 44, alignment: .leading)
                }
                .buttonStyle(.plain)
            }
        }
    }
}

// MARK: - A row's badges: the manager plan, the clock change, the section

/// "Manager plan" (a row the plan placed — never moved by any pass; tap for
/// why), the clock-change badge, and the section (or the usual one, with
/// one tap to assign it).
struct ScheduleRowBadges: View {
    @Bindable var viewModel: LaborViewModel
    let row: ScheduleRow
    @State private var showingWhy = false

    var body: some View {
        let section = viewModel.sections.section(for: row)
        let usual = section == nil && viewModel.sections.isFrontOfHouse(row.role) ? viewModel.sections.usual(for: row) : nil
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 5) {
                if row.isManagerPlan {
                    Button {
                        Haptic.light()
                        showingWhy.toggle()
                    } label: {
                        ScheduleRowTag(text: "Manager plan", tone: .cavnarEmber2, symbol: "pin.fill")
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Shows why the manager plan put them on")
                }
                if let dst = row.dstHours, dst != 0 {
                    ScheduleRowTag(text: dst > 0 ? "+1h clock change" : "\u{2212}1h clock change",
                                   tone: .cavnarAmber, symbol: "clock.arrow.2.circlepath")
                        .accessibilityLabel(dst > 0 ? "Includes the extra hour when the clocks go back"
                                                    : "An hour shorter: the clocks go forward")
                }
                if let section {
                    ScheduleRowTag(text: section, tone: .cavnarInk2, symbol: "square.grid.2x2")
                }
            }
            if showingWhy, let why = row.pinReason {
                HomeMixedText.make(why, size: CavnarType.caption, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let dst = row.dstHours, dst > 0 {
                Text("Includes the extra hour when the clocks go back")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
            }
            if let usual, let name = usual.section {
                HStack(spacing: 6) {
                    HomeMixedText.make("Usually \(name) on \(usual.day ?? "") \(usual.daypart == "morning" ? "lunch" : "dinner")",
                                       size: CavnarType.caption, color: .cavnarInk3)
                    Button {
                        Task { await viewModel.assignSection(row, section: name) }
                    } label: {
                        Text(viewModel.sectionBusy == row.id ? "Assigning" : "Assign")
                            .font(.cavnarBody(CavnarType.caption, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(viewModel.sectionBusy != nil)
                }
            }
        }
    }
}

// MARK: - What each shift was asked for (E: the requirements)

/// The requirements the week was written and scored to: each shift's roles
/// with "(usual N)" when the date moved them, the staffing asks folded in,
/// the late-night row on a late-closing night, and why each moved.
struct ScheduleRequirementsView: View {
    let rows: [RequirementRow]
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                Haptic.selection()
                withAnimation(.easeOut(duration: 0.2)) { open.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Text("What each shift was asked for")
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                    Spacer(minLength: 0)
                    Image(systemName: open ? "chevron.up" : "chevron.down").font(.system(size: 11, weight: .bold))
                }
                .foregroundStyle(Color.cavnarEmber2)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open {
                Text("The people each shift needs, from what you usually run that day scaled by its expected sales. A role that moved shows its usual crew; your floors and staffing asks are folded in.")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(rows) { row in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Text(CavnarDate.dayDate(row.day, row.date) + " \u{00B7} " + row.partLabel)
                                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                .foregroundStyle(Color.cavnarInk)
                            if row.isLate { ScheduleRowTag(text: "Late night", tone: .cavnarInk2, symbol: "moon.fill") }
                        }
                        HomeMixedText.make(row.roles.map(\.line).joined(separator: " \u{00B7} "),
                                           size: CavnarType.secondary, weight: 600, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                        ForEach(row.reasons, id: \.self) { why in
                            HomeMixedText.make(why, size: CavnarType.caption, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        ForEach(row.roles.compactMap(\.reason), id: \.self) { why in
                            HomeMixedText.make(why, size: CavnarType.caption, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.vertical, 4)
                }
                .transition(.opacity)
            }
        }
    }
}

// MARK: - The review's new parts

/// Everything the review now carries beyond its lines, structured: stages
/// that did not run, what the week does not meet by day, the setup to
/// confirm, facts naming nobody, why the trim stopped, nights over the
/// section count, people under their minimum, and the hours by pay.
struct ScheduleReviewExtras: View {
    @Bindable var viewModel: LaborViewModel
    let result: GeneratedSchedule
    var onOpenPerson: (String) -> Void = { _ in }
    var onOpenHours: () -> Void = {}
    @State private var showingAllUnmet = false

    private var review: ScheduleReview? { result.review }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let split = result.hoursSplit, let line = split.line() {
                HomeMixedText.make("\(CavnarQualityFormat.hours(result.hoursScheduled ?? 0))h scheduled \u{00B7} \(line)",
                                   size: CavnarType.caption, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let failures = review?.stageFailures?.items, !failures.isEmpty { stageBlock(failures) }
            if let unmet = review?.unmet?.items, !unmet.isEmpty { unmetBlock(unmet) }
            if let conflict = review?.budgetConflict, conflict.heldLine != nil { budgetBlock(conflict) }
            if let caps = review?.capFloorConflicts?.items, !caps.isEmpty { capBlock(caps) }
            if !result.minHoursLeft.isEmpty { minHoursBlock(result.minHoursLeft) }
            if let setup = review?.setup?.items.filter({ $0.kind != "managers" && $0.text != nil }), !setup.isEmpty {
                setupBlock(setup)
            }
            if let names = review?.unmatchedNames?.items, !names.isEmpty { unmatchedBlock(names) }
        }
    }

    private func kicker(_ text: String, _ color: Color = .cavnarInk3) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(CavnarType.kicker, weight: 700))
            .tracking(1.1)
            .foregroundStyle(color)
    }

    // Repair stages that did not run (P-3, P-17): a blocking one leads with ⚠.
    private func stageBlock(_ failures: [ReviewStageFailure]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            kicker("Checks Cavnar AI couldn\u{2019}t finish", .cavnarRedText)
            ForEach(Array(failures.enumerated()), id: \.offset) { _, f in
                if let text = f.text {
                    HStack(alignment: .top, spacing: 7) {
                        Image(systemName: f.blocksPublish ? "exclamationmark.triangle.fill" : "info.circle")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(f.blocksPublish ? Color.cavnarRed : Color.cavnarInk3)
                            .padding(.top, 3)
                        HomeMixedText.make(text, size: CavnarType.secondary, weight: f.blocksPublish ? 600 : 400,
                                           color: f.blocksPublish ? .cavnarRed : .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    // "What this week doesn't meet" (C2-1), grouped by day.
    private func unmetBlock(_ items: [ReviewUnmetItem]) -> some View {
        let groups = Self.unmetGroups(items)
        let shown = showingAllUnmet ? groups : Array(groups.prefix(4))
        return VStack(alignment: .leading, spacing: 8) {
            kicker("What this week doesn\u{2019}t meet")
            ForEach(shown, id: \.title) { group in
                VStack(alignment: .leading, spacing: 5) {
                    HomeMixedText.make(group.title, size: CavnarType.caption, weight: 700, color: .cavnarInk3)
                    ForEach(Array(group.items.enumerated()), id: \.offset) { _, item in
                        unmetRow(item)
                    }
                }
            }
            if groups.count > 4 {
                Button(showingAllUnmet ? "Show fewer" : "Show all \(groups.count) days") {
                    Haptic.selection()
                    showingAllUnmet.toggle()
                }
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarEmber2)
                .buttonStyle(.plain)
            }
        }
    }

    private func unmetRow(_ item: ReviewUnmetItem) -> some View {
        let tone: Color = {
            switch item.style {
            case .hard: return .cavnarEmber
            case .staffing: return .cavnarEmber2
            case .neutral, .unchecked: return .cavnarInk3
            }
        }()
        return HStack(alignment: .top, spacing: 8) {
            Circle().fill(tone).frame(width: 6, height: 6).padding(.top, 6)
            VStack(alignment: .leading, spacing: 2) {
                HomeMixedText.make((item.what ?? "") + (item.partWords.map { " \u{00B7} \($0)" } ?? ""),
                                   size: CavnarType.secondary, weight: 700, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if let why = item.why {
                    HomeMixedText.make(why, size: CavnarType.caption, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if item.style == .unchecked {
                    Text("Check it yourself")
                        .font(.cavnarBody(CavnarType.caption, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
        }
    }

    struct UnmetGroup { let title: String; let items: [ReviewUnmetItem] }

    /// Items by date ("Fri 10/16/26"), in date order; dateless ones under
    /// "This week" last.
    static func unmetGroups(_ items: [ReviewUnmetItem]) -> [UnmetGroup] {
        var order: [String] = []
        var by: [String: [ReviewUnmetItem]] = [:]
        for item in items {
            let key = item.date.map { String($0.prefix(10)) } ?? ""
            if by[key] == nil { order.append(key) }
            by[key, default: []].append(item)
        }
        let dated = order.filter { !$0.isEmpty }.sorted()
        var out = dated.map { d -> UnmetGroup in
            let day = by[d]?.first?.day ?? LaborViewModel.weekdayName(d)
            return UnmetGroup(title: [day.map { String($0.prefix(3)) }, CavnarDate.mdy(d)].compactMap { $0 }
                .joined(separator: " "), items: by[d] ?? [])
        }
        if let rest = by[""], !rest.isEmpty { out.append(UnmetGroup(title: "This week", items: rest)) }
        return out
    }

    // Why the trim stopped above the budget (SQ-7).
    private func budgetBlock(_ c: ReviewBudgetConflict) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            kicker("What holds the hours over budget", .cavnarAmber)
            if let held = c.heldLine {
                HomeMixedText.make(held, size: CavnarType.secondary, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(c.examples.prefix(6).enumerated()), id: \.offset) { _, e in
                HomeMixedText.make([CavnarDate.dayDate(e.day, e.date), e.label].compactMap { $0 }
                    .filter { !$0.isEmpty }.joined(separator: " \u{00B7} "), size: CavnarType.caption, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // Nights over the section count (P-29).
    private func capBlock(_ caps: [ReviewCapConflict]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            kicker("More on the floor than sections", .cavnarAmber)
            ForEach(Array(caps.prefix(4).enumerated()), id: \.offset) { _, c in
                HomeMixedText.make(c.line, size: CavnarType.secondary, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Your floors or a rule held these over the section count. Lower a floor or raise the section count in Schedule rules.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // Minimum hours left (A2-3).
    private func minHoursBlock(_ left: [MinHoursReport.Left]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            kicker("Under their minimum hours", .cavnarAmber)
            ForEach(Array(left.prefix(6).enumerated()), id: \.offset) { _, l in
                HomeMixedText.make(l.line, size: CavnarType.secondary, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // The setup to confirm (F1-9).
    private func setupBlock(_ items: [ReviewSetupItem]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            kicker("Drafted against")
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                VStack(alignment: .leading, spacing: 4) {
                    HomeMixedText.make(item.text ?? "", size: CavnarType.secondary, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    if item.kind == "leader_rules_inactive", item.canAdopt {
                        if let note = viewModel.adoptNote {
                            Text(note).font(.cavnarBody(CavnarType.caption, weight: 600)).foregroundStyle(Color.cavnarGreen)
                                .fixedSize(horizontal: false, vertical: true)
                        } else {
                            Button {
                                Task { await viewModel.adoptSupportRatings() }
                            } label: {
                                Group {
                                    if viewModel.isAdopting { CavnarShimmerText(text: "Saving") } else { Text("Count them as mine") }
                                }
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                            .disabled(viewModel.isAdopting)
                        }
                    }
                    if item.kind == "close_times_missing" {
                        Button("Set close times in Hours") { onOpenHours() }
                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .buttonStyle(.plain)
                            .frame(minHeight: 44)
                    }
                }
            }
        }
    }

    // Facts filed under a name nobody on the roster goes by (F2-6).
    private func unmatchedBlock(_ names: [ReviewUnmatchedName]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            kicker("Names that match nobody", .cavnarAmber)
            ForEach(Array(names.prefix(8).enumerated()), id: \.offset) { _, u in
                VStack(alignment: .leading, spacing: 2) {
                    HomeMixedText.make(u.detail ?? u.name ?? "", size: CavnarType.secondary, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    if let s = u.suggestion {
                        Button("Open \(s)") { onOpenPerson(s) }
                            .font(.cavnarBody(CavnarType.caption, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .buttonStyle(.plain)
                            .frame(minHeight: 44)
                    }
                }
            }
        }
    }
}

// MARK: - Redo some days (B2, H1-5)

/// The days ticked, why the owner is redoing them (a chip, sent as
/// `reason_chip`) and their own words (≤300, `reason_text`).
struct RedoDaysSheet: View {
    @Bindable var viewModel: LaborViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var reason: String?
    @State private var text = ""

    /// schedule_engine.REDO_REASONS — the key is sent, the words shown.
    static let reasons: [(key: String, label: String)] = [
        ("too_few", "Too few people on"), ("too_many", "Too many people on"),
        ("wrong_people", "The wrong people on"), ("times", "Start or end times are off"),
        ("manager", "Manager coverage is wrong"), ("overtime", "Too much overtime"),
        ("fairness", "Shifts aren\u{2019}t shared fairly"), ("other", "Something else"),
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Redo \(viewModel.selectedRedoDates.count) \(viewModel.selectedRedoDates.count == 1 ? "day" : "days")")
                            .font(.cavnarHeadline(CavnarType.section))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(viewModel.selectedRedoDates.sorted()
                            .map { CavnarDate.dayDate(LaborViewModel.weekdayName($0), $0) }.joined(separator: ", "),
                                           size: CavnarType.secondary, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("Only these days are written again; the rest of the week is kept.")
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    AccountSection(kicker: "What's wrong with them?") {
                        AccountFlowLayout(spacing: 8) {
                            ForEach(Self.reasons, id: \.key) { r in
                                Button(r.label) {
                                    Haptic.selection()
                                    reason = reason == r.key ? nil : r.key
                                }
                                .buttonStyle(RecAnswerPillStyle(selected: reason == r.key))
                            }
                        }
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        TextField("What\u{2019}s wrong? (optional)", text: $text, axis: .vertical)
                            .font(.cavnar(.body))
                            .lineLimit(2...5)
                            .padding(12)
                            .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                                .fill(Color.cavnarPaper2))
                            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                                .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                            .onChange(of: text) { _, t in if t.count > 300 { text = String(t.prefix(300)) } }
                        HomeMixedText.make("\(text.count)/300", size: CavnarType.caption, color: .cavnarInk3)
                    }
                    Button {
                        Haptic.medium()
                        let r = reason, t = text
                        dismiss()
                        Task { await viewModel.redoSelectedDays(reason: r, text: t) }
                    } label: {
                        Label("Redo these days", systemImage: "arrow.clockwise").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.selectedRedoDates.isEmpty))
                    .disabled(viewModel.selectedRedoDates.isEmpty || viewModel.isGeneratingSchedule)
                }
                .padding(20)
            }
            .accountSheetChrome("Redo some days")
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }
}

// MARK: - Why this change? (H1-1)

/// After a save, one tap of why for each big change (L-35): "Always keep
/// Bob off Tuesday dinner" / "Just this week" / "Bob called off". Leaving
/// it unanswered is fine.
struct EditWhySheet: View {
    @Bindable var viewModel: LaborViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Why this change?")
                        .font(.cavnarHeadline(CavnarType.section))
                        .foregroundStyle(Color.cavnarInk)
                    Text("One tap teaches the next draft. Skip any you'd rather not answer.")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk3)
                    ForEach(viewModel.whyQuestions) { q in
                        VStack(alignment: .leading, spacing: 10) {
                            HomeMixedText.make(q.text ?? "", size: CavnarType.body, weight: 600, color: .cavnarInk)
                                .fixedSize(horizontal: false, vertical: true)
                            if let said = viewModel.whyAnswered[q.key] {
                                Label(said, systemImage: "checkmark")
                                    .font(.cavnarBody(CavnarType.secondary, weight: 600))
                                    .foregroundStyle(said == "Saved" ? Color.cavnarGreen : Color.cavnarAmber)
                            } else {
                                AccountFlowLayout(spacing: 8) {
                                    ForEach(q.options, id: \.answer) { o in
                                        Button(o.label) { Task { await viewModel.answerWhy(q, answer: o.answer) } }
                                            .buttonStyle(RecAnswerPillStyle())
                                    }
                                }
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .cavnarCard()
                    }
                    if let error = viewModel.whyError {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                    }
                    Button("Done") { dismiss() }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .frame(maxWidth: .infinity)
                }
                .padding(20)
            }
            .accountSheetChrome("Why this change?")
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .onDisappear { viewModel.dismissWhy() }
    }
}

// MARK: - Publish check: worth a look, likely to change (G-1, H1-6)

struct PublishWorthALookCard: View {
    let notes: [PublishNote]
    let hours: PublishHours?
    let budget: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("WORTH A LOOK")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            if let line = hours?.line(budget: budget) {
                HomeMixedText.make(line, size: CavnarType.secondary, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(notes.enumerated()), id: \.offset) { _, note in
                HStack(alignment: .top, spacing: 8) {
                    Circle().fill(Color.cavnarInk3).frame(width: 5, height: 5).padding(.top, 7)
                    HomeMixedText.make(note.text, size: CavnarType.secondary, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if !notes.isEmpty {
                Text("These never hold the send \u{2014} nothing to acknowledge.")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}

struct PublishLikelyToChangeCard: View {
    let likely: LikelyToChange

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("LIKELY TO CHANGE")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            Text("From your own past edits \u{2014} rows you usually change. Worth a look before staff are told.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(Array(likely.rows.enumerated()), id: \.offset) { _, row in
                HStack(alignment: .top, spacing: 10) {
                    if let l = row.likelihood {
                        Text("\(Int((l * 100).rounded()))%")
                            .font(.cavnarNumber(CavnarType.secondary, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(width: 42, alignment: .leading)
                    }
                    HomeMixedText.make(row.text ?? [row.employee, row.date.map { CavnarDate.mdy($0) }, row.shiftStart]
                        .compactMap { $0 }.joined(separator: " \u{00B7} "), size: CavnarType.secondary, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let note = likely.note {
                HomeMixedText.make(note.prefix(1).uppercased() + note.dropFirst() + ".", size: CavnarType.caption, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}
