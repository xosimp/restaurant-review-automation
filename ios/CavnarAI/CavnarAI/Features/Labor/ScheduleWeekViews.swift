import SwiftUI
import UIKit

// The drafted week read on a phone (iOS parity #47, #48, the PDF row,
// 10/7/26): the web's week grid and list become a day strip with a page per
// day and a per-person week; the scorecard a draft lands on; a PDF to post or
// print. The web keeps its drag-and-drop grid and bulk edits.

// MARK: - The week, a day at a time (#48)

/// One day of the week on the strip: its weekday, date and shift count.
struct ScheduleDayPage: Identifiable, Hashable {
    let day: String
    let date: String?
    let count: Int
    var notWritten = false
    /// A stretch of this day has no manager on the floor — a red dot on
    /// its chip (re-audit 10/8/26 H4).
    var managerGap = false
    var id: String { day }

    /// "Mon" and "10/12" for the chip.
    var short: String { String(day.prefix(3)) }
    var dateLabel: String {
        guard let date else { return "" }
        let mdy = CavnarDate.mdy(date)
        let parts = mdy.split(separator: "/")
        return parts.count == 3 ? "\(parts[0])/\(parts[1])" : mdy
    }
}

/// Who on the week is salaried (lowercased names), from the managers'
/// status the rules carry — the ">40h · overtime at 1.5×" mark is for
/// hourly pay only (re-audit 10/8/26 L8). Empty when unknown: the mark
/// then falls back to every person past 40h, as before.
private struct ScheduleSalariedNamesKey: EnvironmentKey {
    static let defaultValue: Set<String> = []
}

extension EnvironmentValues {
    var scheduleSalariedNames: Set<String> {
        get { self[ScheduleSalariedNamesKey.self] }
        set { self[ScheduleSalariedNamesKey.self] = newValue }
    }
}

/// One person's week, from the rows on screen.
struct PersonWeek: Identifiable, Hashable {
    let name: String
    let roles: [String]
    let rows: [ScheduleRow]
    var id: String { name }

    var hours: Double { rows.reduce(0) { $0 + ScheduleWeekMath.hours(of: $1) } }
    var days: Int { Set(rows.compactMap(\.date)).count }

    /// Past 40 hours on hourly pay — overtime at 1.5×. A salaried person's
    /// long week costs no overtime, so it is never marked (L8).
    func overtime(salaried: Set<String>) -> Bool {
        hours > 40 && !salaried.contains(name.lowercased())
    }

    static func == (a: PersonWeek, b: PersonWeek) -> Bool { a.name == b.name && a.rows.map(\.id) == b.rows.map(\.id) }
    func hash(into h: inout Hasher) { h.combine(name); h.combine(rows.map(\.id)) }
}

enum ScheduleWeekMath {
    static let dayOrder = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    /// A row's hours: its own figure, else worked out from its times.
    static func hours(of row: ScheduleRow) -> Double {
        if let h = row.scheduledHours.flatMap({ Double($0) }), h.isFinite { return h }
        if let s = row.shiftStart, let e = row.shiftEnd, let text = LaborViewModel.shiftHours(s, e) {
            return Double(text) ?? 0
        }
        return 0
    }

    /// Everyone on the week, most hours first, each with their shifts in
    /// day order — the per-person view.
    static func people(_ rows: [ScheduleRow]) -> [PersonWeek] {
        var by: [String: [ScheduleRow]] = [:]
        var spelled: [String: String] = [:]
        for r in rows {
            let name = (r.employee ?? "").trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty else { continue }
            let key = name.lowercased()
            by[key, default: []].append(r)
            if spelled[key] == nil { spelled[key] = name }
        }
        return by.map { key, rows in
            let sorted = rows.sorted { ($0.date ?? "", LaborViewModel.shiftMinutes($0.shiftStart) ?? 0)
                < ($1.date ?? "", LaborViewModel.shiftMinutes($1.shiftStart) ?? 0) }
            var roles: [String] = []
            for r in sorted { if let role = r.role, !role.isEmpty, !roles.contains(role) { roles.append(role) } }
            return PersonWeek(name: spelled[key] ?? key, roles: roles, rows: sorted)
        }
        .sorted { $0.hours != $1.hours ? $0.hours > $1.hours : $0.name < $1.name }
    }

    static func hoursText(_ h: Double) -> String {
        h == h.rounded() ? String(Int(h)) : String(format: "%.1f", h)
    }

    /// The weekday a row falls on: its own day, else its date's.
    static func dayName(of row: ScheduleRow) -> String? {
        if let day = row.day, !day.isEmpty { return day }
        return row.date.flatMap(LaborViewModel.weekdayName)
    }

    /// One cell of the iPad week grid (#99): this person's shifts on this
    /// day, earliest first (the person's rows are already in day order).
    static func shifts(_ person: PersonWeek, on day: String) -> [ScheduleRow] {
        person.rows.filter { dayName(of: $0) == day }
    }
}

/// The week as a strip of day chips over one day's page, or everyone's
/// week. Swipe the page sideways for the next or previous day. `dayContent`
/// draws a day (LaborView's editable rows, or History's read-only ones).
struct ScheduleWeekPager<DayContent: View>: View {
    enum Mode: String, CaseIterable, Hashable { case week = "Week", day = "By day", person = "By person" }

    /// The segments on offer. The person × day grid (#99) needs an iPad's
    /// regular width; the phone keeps By day / By person.
    static func modes(wide: Bool) -> [Mode] {
        wide ? [.week, .day, .person] : [.day, .person]
    }

    /// What is drawn: the one picked, else the grid on a wide screen and
    /// the day pager on a phone — and never the grid once the width
    /// narrows (Split View), which falls back to the day pager.
    static func resolvedMode(_ chosen: Mode?, wide: Bool) -> Mode {
        guard let chosen else { return wide ? .week : .day }
        return modes(wide: wide).contains(chosen) ? chosen : .day
    }

    let days: [ScheduleDayPage]
    let rows: [ScheduleRow]
    @Binding var selectedDay: String?
    /// A shift tapped in a person's week opens its day here.
    var onPickShift: ((ScheduleRow) -> Void)? = nil
    /// A shift tapped in the iPad week grid: Labor's editor opens it in
    /// ShiftEditSheet. Nil (History, a replaced copy): the tap opens its day.
    var onEditShift: ((ScheduleRow) -> Void)? = nil
    // Last, so a caller's trailing closure is the day's page.
    @ViewBuilder let dayContent: (ScheduleDayPage) -> DayContent

    @State private var chosenMode: Mode?
    @State private var person: PersonWeek?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.horizontalSizeClass) private var sizeClass
    @Environment(\.scheduleSalariedNames) private var salariedNames

    private var wide: Bool { CavnarLayout.isWide(sizeClass) }
    private var mode: Mode { Self.resolvedMode(chosenMode, wide: wide) }
    private var modeBinding: Binding<Mode> {
        Binding(get: { mode }, set: { chosenMode = $0 })
    }

    private var current: ScheduleDayPage? {
        days.first { $0.day == selectedDay } ?? days.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            CavnarSegmentedControl(selection: modeBinding, options: Self.modes(wide: wide), accessibilityTitle: "Schedule view") { $0.rawValue }
            if mode == .week {
                ScheduleWeekGrid(days: days, rows: rows) { row in
                    if let onEditShift {
                        onEditShift(row)
                    } else if let day = ScheduleWeekMath.dayName(of: row) {
                        chosenMode = .day
                        selectedDay = day
                    }
                }
            } else if mode == .day {
                dayStrip
                if let page = current {
                    dayContent(page)
                        .id(page.id)
                        .transition(.opacity)
                        .contentShape(Rectangle())
                        .simultaneousGesture(
                            DragGesture(minimumDistance: 24).onEnded { value in
                                guard abs(value.translation.width) > 60,
                                      abs(value.translation.width) > abs(value.translation.height) * 1.5 else { return }
                                step(value.translation.width < 0 ? 1 : -1)
                            })
                        .accessibilityAction(named: "Next day") { step(1) }
                        .accessibilityAction(named: "Previous day") { step(-1) }
                }
            } else {
                peopleList
            }
        }
        .onAppear { if selectedDay == nil || !days.contains(where: { $0.day == selectedDay }) { selectedDay = days.first?.day } }
        .sheet(item: $person) { p in
            PersonWeekSheet(person: p) { row in
                person = nil
                if let day = row.day ?? row.date.flatMap(LaborViewModel.weekdayName) {
                    chosenMode = .day
                    selectedDay = day
                }
                onPickShift?(row)
            }
        }
    }

    private func step(_ by: Int) {
        guard let page = current, let i = days.firstIndex(of: page) else { return }
        let next = i + by
        guard days.indices.contains(next) else { return }
        Haptic.selection()
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.22)) { selectedDay = days[next].day }
    }

    private var dayStrip: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(days) { page in
                        let on = page.id == current?.id
                        Button {
                            Haptic.selection()
                            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) { selectedDay = page.day }
                        } label: {
                            VStack(spacing: 2) {
                                Text(page.short)
                                    .font(.cavnar(.kicker))
                                    .textCase(.uppercase)
                                Text(page.dateLabel)
                                    .font(.cavnar(.figureS))
                                    .minimumScaleFactor(0.85)
                                    .lineLimit(1)
                                Group {
                                    if page.notWritten {
                                        Image(systemName: "exclamationmark.triangle.fill")
                                            .font(.cavnar(.caption))
                                            .foregroundStyle(Color.cavnarAmber)
                                    } else {
                                        Text("\(page.count)")
                                            .font(.cavnarNumber(CavnarType.caption, weight: 600))
                                    }
                                }
                                .opacity(on ? 1 : 0.75)
                            }
                            .foregroundStyle(on ? Color.cavnarInk : Color.cavnarInk2)
                            // Grows with Dynamic Type (M25): a floor, never
                            // a fixed box that clips the date.
                            .padding(.horizontal, CavnarSpace.xs)
                            .padding(.vertical, CavnarSpace.xxs + 2)
                            .frame(minWidth: 54, minHeight: 62)
                            .overlay(alignment: .topTrailing) {
                                if page.managerGap {
                                    Circle()
                                        .fill(Color.cavnarRed)
                                        .frame(width: 8, height: 8)
                                        .padding(5)
                                        .accessibilityHidden(true)
                                }
                            }
                            .background(
                                RoundedRectangle(cornerRadius: 12, style: .continuous)
                                    .fill(on ? Color.cavnarEmber.opacity(0.22) : Color.cavnarPaper2.opacity(0.6)))
                            .overlay(
                                RoundedRectangle(cornerRadius: 12, style: .continuous)
                                    .strokeBorder(on ? Color.cavnarEmber : Color.cavnarPaper3.opacity(0.7),
                                                  lineWidth: on ? 1.5 : 1))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .id(page.id)
                        .accessibilityLabel("\(page.day) \(page.date.map(CavnarDate.mdy) ?? ""), "
                                            + (page.notWritten ? "not written" : "\(page.count) shifts")
                                            + (page.managerGap ? ", no manager for part of the day" : ""))
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
                .padding(.vertical, 2)
            }
            .onChange(of: selectedDay) { _, day in
                guard let day else { return }
                withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) { proxy.scrollTo(day, anchor: .center) }
            }
        }
    }

    private var peopleList: some View {
        let people = ScheduleWeekMath.people(rows)
        return VStack(spacing: 0) {
            if people.isEmpty {
                Text("Nobody is on this week yet.")
                    .font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 8)
            }
            ForEach(people) { p in
                Button {
                    Haptic.light()
                    person = p
                } label: {
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(p.name).font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
                            if !p.roles.isEmpty {
                                Text(p.roles.joined(separator: ", "))
                                    .font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        Spacer(minLength: 8)
                        VStack(alignment: .trailing, spacing: 2) {
                            (Text(ScheduleWeekMath.hoursText(p.hours)).font(.cavnarNumber(CavnarType.body, weight: 700))
                             + Text("h").font(.cavnarNumber(CavnarType.caption, weight: 600)))
                                .foregroundStyle(p.overtime(salaried: salariedNames) ? Color.cavnarRedText : Color.cavnarInk)
                            Text("\(p.days) \(p.days == 1 ? "day" : "days")")
                                .font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk3)
                        }
                        Image(systemName: "chevron.right")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    .padding(.vertical, 10)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(p.name), \(ScheduleWeekMath.hoursText(p.hours)) hours over \(p.days) days")
                if p.id != people.last?.id {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                }
            }
        }
        .cavnarCard()
    }
}

// MARK: - The week as a grid (iPad, parity audit #99)

/// Everyone × every day on an iPad's regular width — the web's week grid
/// as a read view. Each shift is a chip (times in the number face, the
/// role under it, amber when the rules check flagged it); a tap edits it
/// in ShiftEditSheet (Labor) or opens its day (History). Wider than the
/// column? It scrolls sideways; the names stay readable.
struct ScheduleWeekGrid: View {
    let days: [ScheduleDayPage]
    let rows: [ScheduleRow]
    var onTap: (ScheduleRow) -> Void
    @Environment(\.scheduleSalariedNames) private var salariedNames

    static let nameWidth: CGFloat = 150
    static let dayWidth: CGFloat = 112

    var body: some View {
        let people = ScheduleWeekMath.people(rows)
        VStack(alignment: .leading, spacing: 0) {
            if people.isEmpty {
                Text("Nobody is on this week yet.")
                    .font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 8)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    Grid(alignment: .topLeading, horizontalSpacing: 6, verticalSpacing: 8) {
                        GridRow {
                            CavnarKicker("Who", isHeader: false)
                                .frame(width: Self.nameWidth, alignment: .leading)
                            ForEach(days) { page in dayHeader(page) }
                        }
                        ForEach(people) { person in
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                                .gridCellUnsizedAxes(.horizontal)
                            GridRow(alignment: .top) {
                                personCell(person)
                                ForEach(days) { page in
                                    dayCell(person: person, day: page.day)
                                }
                            }
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
        .cavnarCard()
    }

    private func dayHeader(_ page: ScheduleDayPage) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            CavnarKicker(page.short, isHeader: false)
            HStack(spacing: 5) {
                Text(page.dateLabel)
                    .font(.cavnarNumber(CavnarType.caption, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                if page.notWritten {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Color.cavnarAmber)
                } else {
                    Text("\(page.count)")
                        .font(.cavnarNumber(CavnarType.tag, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
        }
        .frame(width: Self.dayWidth, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(page.day) \(page.date.map(CavnarDate.mdy) ?? ""), "
                            + (page.notWritten ? "not written" : "\(page.count) shifts"))
    }

    private func personCell(_ person: PersonWeek) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(person.name)
                .font(.cavnarBody(CavnarType.secondary, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .lineLimit(2)
            (Text(ScheduleWeekMath.hoursText(person.hours)).font(.cavnarNumber(CavnarType.caption, weight: 700))
             + Text("h").font(.cavnarNumber(CavnarType.tag, weight: 600)))
                .foregroundStyle(person.overtime(salaried: salariedNames) ? Color.cavnarRedText : Color.cavnarInk2)
        }
        .frame(width: Self.nameWidth, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private func dayCell(person: PersonWeek, day: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(ScheduleWeekMath.shifts(person, on: day)) { row in
                shiftChip(row, person: person.name, day: day)
            }
        }
        .frame(width: Self.dayWidth, alignment: .topLeading)
    }

    private func shiftChip(_ row: ScheduleRow, person: String, day: String) -> some View {
        let flagged = row.needsReview == true
        let tone: Color = flagged ? .cavnarAmber : .cavnarEmber
        return Button {
            Haptic.light()
            onTap(row)
        } label: {
            VStack(alignment: .leading, spacing: 1) {
                Text("\(row.shiftStart ?? "")\u{2013}\(row.shiftEnd ?? "")")
                    .font(.cavnarNumber(CavnarType.caption, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                if let role = row.role, !role.isEmpty {
                    Text(role)
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineLimit(1)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .frame(width: Self.dayWidth, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(tone.opacity(0.14)))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(tone.opacity(0.45), lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .cavnarHoverCard(cornerRadius: 8)
        .accessibilityLabel("\(person), \(day), \(row.shiftStart ?? "") to \(row.shiftEnd ?? "")"
                            + (row.role.map { ", \($0)" } ?? "") + (flagged ? ", needs review" : ""))
    }
}

/// One person's week: each shift by day, their hours and days on.
struct PersonWeekSheet: View {
    let person: PersonWeek
    var onPick: ((ScheduleRow) -> Void)? = nil
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scheduleSalariedNames) private var salariedNames

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack(spacing: 18) {
                        stat(ScheduleWeekMath.hoursText(person.hours) + "h", "this week",
                             tone: person.overtime(salaried: salariedNames) ? .cavnarRedText : .cavnarInk)
                        stat("\(person.days)", person.days == 1 ? "day on" : "days on", tone: .cavnarInk)
                        stat("\(person.rows.count)", person.rows.count == 1 ? "shift" : "shifts", tone: .cavnarInk)
                    }
                    if person.overtime(salaried: salariedNames) {
                        HomeMixedText.make("Past 40h \u{2014} overtime at 1.5\u{00D7} on the hours over.",
                                           size: CavnarType.secondary, weight: 600, color: .cavnarRedText)
                    }
                    VStack(spacing: 0) {
                        ForEach(person.rows) { row in
                            Button {
                                Haptic.light()
                                if let onPick { onPick(row) } else { dismiss() }
                            } label: {
                                HStack(alignment: .firstTextBaseline, spacing: 10) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(row.day ?? row.date.flatMap(LaborViewModel.weekdayName) ?? "")
                                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                            .foregroundStyle(Color.cavnarInk)
                                        HomeMixedText.make([row.date.map(CavnarDate.mdy), row.role].compactMap { $0 }
                                                            .filter { !$0.isEmpty }.joined(separator: " \u{00B7} "),
                                                           size: CavnarType.caption, color: .cavnarInk3)
                                    }
                                    Spacer(minLength: 8)
                                    VStack(alignment: .trailing, spacing: 2) {
                                        Text("\(row.shiftStart ?? "")\u{2013}\(row.shiftEnd ?? "")")
                                            .font(.cavnarNumber(CavnarType.secondary, weight: 600))
                                            .foregroundStyle(Color.cavnarInk2)
                                        Text(ScheduleWeekMath.hoursText(ScheduleWeekMath.hours(of: row)) + "h")
                                            .font(.cavnarNumber(CavnarType.caption))
                                            .foregroundStyle(Color.cavnarInk3)
                                    }
                                }
                                .padding(.vertical, 11)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            if row.id != person.rows.last?.id {
                                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                            }
                        }
                    }
                    .cavnarCard()
                    if onPick != nil {
                        Text("Tap a shift to open its day.")
                            .font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome(person.name)
        }
        .presentationDetents([.medium, .large])
        .cavnarFormSheet()
    }

    private func stat(_ value: String, _ label: String, tone: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.cavnarNumber(CavnarText.figureM.size, weight: 700)).foregroundStyle(tone)
            Text(label).font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
        }
    }
}

// MARK: - The scorecard a draft lands on (#47)

/// The six tiles the web's Studio opens a finished draft on
/// (ssRenderSummary): Shift quality, Labor, Coverage, Overtime, Warnings and
/// Estimated savings — the last a PROJECTION, tagged so, never summed into
/// anything (CLAUDE.md, value delivered). An unmeasured tile reads "—" with
/// why, never 0.
struct ScheduleSummaryTiles {
    struct Tile: Equatable {
        let key: String
        let label: String
        let value: String?      // nil → "—"
        let unit: String
        let sub: String
        let tone: Tone
    }
    enum Tone: Equatable { case good, warn, bad, neutral, hero }

    /// Where the Warnings tile reads from. A fresh draft's scorecard reads
    /// the review saved with it (`.review`); a week reopened from History
    /// reads the publish gate as it stands NOW (`.publishCheck`), because
    /// time off approved or rules changed since the draft make the saved
    /// review stale (re-audit 10/8/26 #4). Nil check: not read (yet).
    enum WarningsSource { case review, publishCheck(PublishCheck?) }

    static func tiles(_ r: GeneratedSchedule, warnings source: WarningsSource = .review) -> [Tile] {
        var out: [Tile] = []
        let q = r.quality
        let score = q?.checked == true ? q?.score : nil
        let band = (q?.band ?? "").isEmpty ? nil : q?.band
        out.append(Tile(key: "quality", label: "Shift quality", value: score.map(String.init), unit: "",
                        sub: band.map { $0.prefix(1).uppercased() + $0.dropFirst() + " \u{00B7} out of 100" }
                            ?? "Not graded yet",
                        tone: score.map { $0 >= 80 ? .good : ($0 >= 60 ? .warn : .bad) } ?? .neutral))
        let lv = r.laborView
        let basis = lv.map { $0.isAllIn ? "all-in" : "hourly pay only" } ?? ""
        if let pct = lv?.pct {
            var sub = "of forecast sales, \(basis)"
            if let t = lv?.targetPct {
                sub += (lv?.targetBasis == "hourly" ? " \u{00B7} the hourly budget is " : " \u{00B7} your target is ")
                    + "\(ScheduleBuildSettings.pct(t))%"
            }
            // Any amount above the target is over — it is a ceiling (M1).
            let over = (lv?.targetPct).map { (pct * 10).rounded() / 10 > $0 } ?? false
            out.append(Tile(key: "labor", label: "Labor", value: String(format: "%.1f", pct), unit: "%", sub: sub,
                            tone: over ? .bad : .good))
        } else {
            out.append(Tile(key: "labor", label: "Labor", value: nil, unit: "",
                            sub: "Generate a new draft to see it",
                            tone: .neutral))
        }
        let cov = q?.dimensions?.first { $0.key == "coverage" }?.score
        out.append(Tile(key: "coverage", label: "Coverage", value: cov.map(String.init), unit: cov == nil ? "" : "%",
                        sub: "of each shift\u{2019}s needs met",
                        tone: cov.map { $0 >= 90 ? .good : ($0 >= 75 ? .warn : .bad) } ?? .neutral))
        // Overtime only from a figure the server priced: no projection, or
        // one without overtime hours, is "—", never a green "Nobody past
        // 40h" (re-audit 10/8/26 #5).
        if let pc = r.projectedCost, let ot = pc.overtimeHours {
            let premium = pc.overtimePremium.map { "\(money($0)) in premium pay" } ?? "Premium pay not priced"
            out.append(Tile(key: "overtime", label: "Overtime", value: ScheduleWeekMath.hoursText(ot), unit: "h",
                            sub: ot > 0 ? premium : "Nobody past 40h",
                            tone: ot > 0 ? .bad : .good))
        } else {
            out.append(Tile(key: "overtime", label: "Overtime", value: nil, unit: "",
                            sub: r.projectedCost == nil ? "Priced once saved"
                                : "Not priced this week",
                            tone: .neutral))
        }
        out.append(warningsTile(r, source))
        if let sav = lv?.savings, let rec = lv?.recentPct, let rev = r.forecastSales, rev > 0 {
            out.append(Tile(key: "savings", label: "Under your recent labor", value: money(sav), unit: "",
                            sub: "vs your last \(lv?.recentDays ?? 14) days at \(ScheduleBuildSettings.pct(rec))% (\(basis)), on \(money(rev)) forecast sales",
                            tone: .hero))
        } else {
            out.append(Tile(key: "savings", label: "Under your recent labor", value: nil, unit: "",
                            sub: "Needs your recent labor % and a forecast",
                            tone: .hero))
        }
        return out
    }

    /// The Warnings tile. A rules check that never came back is "—", never
    /// a green "0 · Every rule kept" (re-audit 10/8/26 #4).
    static func warningsTile(_ r: GeneratedSchedule, _ source: WarningsSource) -> Tile {
        switch source {
        case .review:
            guard let review = r.review, review.hard != nil || review.soft != nil else {
                return Tile(key: "warnings", label: "Warnings", value: nil, unit: "",
                            sub: "No rules check came back \u{2014} review before sending",
                            tone: .neutral)
            }
            let hard = review.hardCount, soft = review.softCount
            return Tile(key: "warnings", label: "Warnings", value: String(hard + soft), unit: "",
                        sub: hard > 0 ? "\(hard) break a hard rule \u{00B7} fix before sending"
                            : (soft > 0 ? "Soft rules only \u{00B7} worth a look" : "Every rule kept"),
                        tone: hard > 0 ? .bad : (soft > 0 ? .warn : .good))
        case .publishCheck(let check):
            guard let check, check.ok else {
                return Tile(key: "warnings", label: "Warnings", value: nil, unit: "",
                            sub: "Read from the publish check \u{2014} not read yet", tone: .neutral)
            }
            if check.replacedReason != nil {
                return Tile(key: "warnings", label: "Warnings", value: nil, unit: "",
                            sub: "A newer copy of this week replaced it", tone: .neutral)
            }
            let blockers = check.shown.lines.count, notes = check.notes.count
            return Tile(key: "warnings", label: "Warnings", value: String(blockers + notes), unit: "",
                        sub: blockers > 0 ? "\(blockers) to read before sending"
                            : (notes > 0 ? "Notes only \u{00B7} worth a look" : "Nothing to read first"),
                        tone: blockers > 0 ? .bad : (notes > 0 ? .warn : .good))
        }
    }

    /// "Week of 10/12/26 – 10/18/26 · 84 shifts · 23 people · 612h"
    static func headline(_ r: GeneratedSchedule) -> String {
        let rows = r.previewRows ?? []
        let people = Set(rows.compactMap { ($0.employee ?? "").lowercased() }.filter { !$0.isEmpty }).count
        let hours = rows.reduce(0) { $0 + ScheduleWeekMath.hours(of: $1) }
        var parts: [String] = []
        if let a = r.weekDates?.first, let b = r.weekDates?.last { parts.append("Week of \(CavnarDate.mdy(a)) \u{2013} \(CavnarDate.mdy(b))") }
        parts.append("\(rows.count) shifts")
        parts.append("\(people) people")
        parts.append("\(ScheduleWeekMath.hoursText(hours))h")
        return parts.joined(separator: " \u{00B7} ")
    }

    static func money(_ v: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.locale = Locale(identifier: "en_US")
        f.currencyCode = "USD"
        f.maximumFractionDigits = 0
        return f.string(from: NSNumber(value: v)) ?? "$\(Int(v.rounded()))"
    }
}

/// The scorecard sheet: "The week is built", six tiles, then View the
/// schedule (the one primary), Publish and Optimize again.
struct ScheduleSummarySheet: View {
    let result: GeneratedSchedule
    let onView: () -> Void
    let onWarnings: () -> Void
    let onPublish: () -> Void
    let onOptimize: (() -> Void)?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: "checkmark")
                            .font(.system(size: 16, weight: .heavy))
                            .foregroundStyle(Color.cavnarEmber)
                            .frame(width: 40, height: 40)
                            .background(Circle().fill(Color.cavnarEmber.opacity(0.16)))
                            .overlay(Circle().strokeBorder(Color.cavnarEmber.opacity(0.4), lineWidth: 1))
                        VStack(alignment: .leading, spacing: 4) {
                            CavnarKicker("Schedule complete")
                            Text("The week is built")
                                .font(.cavnarHeadline(CavnarText.title.size))
                                .foregroundStyle(Color.cavnarInk)
                            HomeMixedText.make(ScheduleSummaryTiles.headline(result), size: CavnarType.secondary, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)],
                              spacing: 10) {
                        ForEach(ScheduleSummaryTiles.tiles(result), id: \.key) { tile in
                            tileView(tile)
                        }
                    }
                    VStack(spacing: 10) {
                        Button {
                            Haptic.light()
                            dismiss()
                            onView()
                        } label: { Text("View the schedule").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarPrimaryButtonStyle())
                        HStack(spacing: 10) {
                            if let onOptimize {
                                Button {
                                    Haptic.light()
                                    dismiss()
                                    onOptimize()
                                } label: { Text("Optimize again").frame(maxWidth: .infinity) }
                                .buttonStyle(CavnarSecondaryButtonStyle())
                            }
                            Button {
                                Haptic.light()
                                dismiss()
                                onPublish()
                            } label: { Text("Publish").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                    }
                    Text("Optimize again makes legal changes that raise the score and lists each one. Publish shows who it reaches and what to read before anything goes out.")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(20)
            }
            .accountSheetChrome("Draft ready")
        }
        .presentationDetents([.large])
    }

    @ViewBuilder
    private func tileView(_ tile: ScheduleSummaryTiles.Tile) -> some View {
        if tile.key == "warnings", tile.value != "0" {
            Button {
                Haptic.light()
                dismiss()
                onWarnings()
            } label: { ScheduleTileCard(tile: tile, showsGo: true) }
            .buttonStyle(.plain)
            .accessibilityLabel("See the \(tile.value ?? "") warnings on the schedule")
        } else {
            ScheduleTileCard(tile: tile)
        }
    }
}

/// One scorecard tile: kicker, the figure ("—" when unmeasured), what it is,
/// and the savings tile's "Projection · not yet earned" tag.
struct ScheduleTileCard: View {
    let tile: ScheduleSummaryTiles.Tile
    var showsGo = false

    private var tone: Color {
        switch tile.tone {
        case .good: return .cavnarGreen
        case .warn: return .cavnarAmber
        case .bad: return .cavnarRed
        case .neutral: return .cavnarInk3
        case .hero: return .cavnarEmber
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            CavnarKicker(tile.label, isHeader: false)
            HStack(alignment: .firstTextBaseline, spacing: 1) {
                Text(tile.value ?? "\u{2014}")
                    .font(.cavnarNumber(CavnarText.figureM.size, weight: 700))
                    .foregroundStyle(tile.value == nil ? Color.cavnarInk3 : (tile.tone == .neutral ? Color.cavnarInk : tone))
                if tile.value != nil, !tile.unit.isEmpty {
                    Text(tile.unit).font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk3)
                }
            }
            HomeMixedText.make(tile.sub, size: CavnarType.caption, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if tile.key == "savings" {
                ScheduleRowTag(text: "Projection \u{00B7} not yet earned", tone: .cavnarEmber2)
            }
            if showsGo {
                HStack(spacing: 3) {
                    Text("See them").font(.cavnarBody(CavnarType.caption, weight: 700))
                    Image(systemName: "arrow.right").font(.system(size: 10, weight: .bold))
                }
                .foregroundStyle(Color.cavnarEmber2)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 128, alignment: .topLeading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous)
            .fill(tile.tone == .hero ? Color.cavnarEmber.opacity(0.1) : Color.cavnarPaper2.opacity(0.7)))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
            .strokeBorder(tile.value == nil ? Color.cavnarPaper3.opacity(0.6) : tone.opacity(0.35), lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

// MARK: - A PDF to post or print

/// The week as a PDF on light paper (the printed-form palette, the app's
/// own tokens in the light appearance): a page per day, each shift a line —
/// name, role, times, hours — for the back-of-house wall or the printer.
/// Drawn with ImageRenderer; shared through ShareLink (print from there).
enum SchedulePDF {
    static let pageSize = CGSize(width: 612, height: 792)       // US Letter, points
    static let rowsPerPage = 26

    struct Page: Identifiable {
        let id: Int
        let day: String
        let date: String?
        let rows: [ScheduleRow]
        let continued: Bool
    }

    /// The pages: each day, split when it runs past a page.
    static func pages(_ rows: [ScheduleRow]) -> [Page] {
        let grouped = Dictionary(grouping: rows) { $0.day ?? $0.date.flatMap(LaborViewModel.weekdayName) ?? "" }
        var out: [Page] = []
        for day in ScheduleWeekMath.dayOrder + grouped.keys.filter({ !ScheduleWeekMath.dayOrder.contains($0) }).sorted() {
            guard let dayRows = grouped[day], !dayRows.isEmpty else { continue }
            let sorted = dayRows.sorted { (LaborViewModel.shiftMinutes($0.shiftStart) ?? 0, $0.role ?? "", $0.employee ?? "")
                < (LaborViewModel.shiftMinutes($1.shiftStart) ?? 0, $1.role ?? "", $1.employee ?? "") }
            var start = 0
            while start < sorted.count {
                let chunk = Array(sorted[start..<min(sorted.count, start + rowsPerPage)])
                out.append(Page(id: out.count, day: day, date: chunk.first?.date, rows: chunk, continued: start > 0))
                start += rowsPerPage
            }
        }
        return out
    }

    /// Writes the PDF to a temporary file and returns it; nil with no rows.
    @MainActor
    static func file(for result: GeneratedSchedule, restaurant: String? = nil) -> URL? {
        let rows = result.previewRows ?? []
        let pages = pages(rows)
        guard !pages.isEmpty else { return nil }
        let week = result.weekDates?.first ?? "week"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("schedule-\(week).pdf")
        var box = CGRect(origin: .zero, size: pageSize)
        guard let ctx = CGContext(url as CFURL, mediaBox: &box, nil) else { return nil }
        let range = result.weekDates.flatMap { d in d.first.flatMap { a in d.last.map { CavnarDate.mdyRange(a, $0) } } }
        for page in pages {
            let renderer = ImageRenderer(content:
                SchedulePDFPage(page: page, weekRange: range, restaurant: restaurant, total: pages.count)
                    .frame(width: pageSize.width, height: pageSize.height)
                    .environment(\.colorScheme, .light))
            renderer.proposedSize = ProposedViewSize(pageSize)
            ctx.beginPDFPage(nil)
            renderer.render { _, draw in draw(ctx) }
            ctx.endPDFPage()
        }
        ctx.closePDF()
        return url
    }
}

private struct SchedulePDFPage: View {
    let page: SchedulePDF.Page
    let weekRange: String?
    let restaurant: String?
    let total: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("Cavnar AI").font(.cavnarBody(11, weight: 700)).foregroundStyle(Color.cavnarInk3)
                Spacer()
                Text("SCHEDULE").font(.cavnarBody(10, weight: 700)).tracking(1.4).foregroundStyle(Color.cavnarEmber2)
            }
            Text(page.day + (page.continued ? " (continued)" : ""))
                .font(.cavnarHeadline(26))
                .foregroundStyle(Color.cavnarInk)
                .padding(.top, 14)
            Rectangle().fill(Color.cavnarEmber).frame(width: 38, height: 2).padding(.top, 6)
            HStack(spacing: 6) {
                if let date = page.date { Text(CavnarDate.mdy(date)).font(.cavnarNumber(12, weight: 600)) }
                if let weekRange { Text("\u{00B7} week of \(weekRange)").font(.cavnarBody(12)) }
                if let restaurant, !restaurant.isEmpty { Text("\u{00B7} \(restaurant)").font(.cavnarBody(12)) }
            }
            .foregroundStyle(Color.cavnarInk2)
            .padding(.top, 8)
            HStack {
                Text("NAME").frame(maxWidth: .infinity, alignment: .leading)
                Text("ROLE").frame(width: 140, alignment: .leading)
                Text("SHIFT").frame(width: 130, alignment: .leading)
                Text("HOURS").frame(width: 50, alignment: .trailing)
            }
            .font(.cavnarBody(9, weight: 700))
            .tracking(0.8)
            .foregroundStyle(Color.cavnarPaper)
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .background(Color.cavnarInk)
            .padding(.top, 16)
            ForEach(Array(page.rows.enumerated()), id: \.offset) { i, row in
                HStack {
                    Text(row.employee ?? "").font(.cavnarBody(11.5, weight: 600)).frame(maxWidth: .infinity, alignment: .leading)
                    Text(row.role ?? "").font(.cavnarBody(11)).frame(width: 140, alignment: .leading)
                    Text("\(row.shiftStart ?? "")\u{2013}\(row.shiftEnd ?? "")").font(.cavnarNumber(11)).frame(width: 130, alignment: .leading)
                    Text(ScheduleWeekMath.hoursText(ScheduleWeekMath.hours(of: row))).font(.cavnarNumber(11)).frame(width: 50, alignment: .trailing)
                }
                .lineLimit(1)
                .foregroundStyle(Color.cavnarInk)
                .padding(.horizontal, 8)
                .frame(height: 20)
                .background(i % 2 == 1 ? Color.cavnarPaper2 : Color.cavnarPaper)
            }
            Spacer(minLength: 0)
            HStack {
                Text("Printed \(CavnarDate.mdy(Date()))").font(.cavnarBody(9)).foregroundStyle(Color.cavnarInk3)
                Spacer()
                Text("\(page.id + 1) of \(total)").font(.cavnarNumber(9)).foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(40)
        .background(Color.cavnarPaper)
    }
}
