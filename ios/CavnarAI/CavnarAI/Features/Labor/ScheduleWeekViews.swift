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

/// One person's week, from the rows on screen.
struct PersonWeek: Identifiable, Hashable {
    let name: String
    let roles: [String]
    let rows: [ScheduleRow]
    var id: String { name }

    var hours: Double { rows.reduce(0) { $0 + ScheduleWeekMath.hours(of: $1) } }
    var days: Int { Set(rows.compactMap(\.date)).count }

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
}

/// The week as a strip of day chips over one day's page, or everyone's
/// week. Swipe the page sideways for the next or previous day. `dayContent`
/// draws a day (LaborView's editable rows, or History's read-only ones).
struct ScheduleWeekPager<DayContent: View>: View {
    enum Mode: String, CaseIterable, Hashable { case day = "By day", person = "By person" }

    let days: [ScheduleDayPage]
    let rows: [ScheduleRow]
    @Binding var selectedDay: String?
    @ViewBuilder let dayContent: (ScheduleDayPage) -> DayContent
    /// A shift tapped in a person's week opens its day here.
    var onPickShift: ((ScheduleRow) -> Void)? = nil

    @State private var mode: Mode = .day
    @State private var person: PersonWeek?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var current: ScheduleDayPage? {
        days.first { $0.day == selectedDay } ?? days.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            CavnarSegmentedControl(selection: $mode, options: Mode.allCases) { $0.rawValue }
            if mode == .day {
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
                    mode = .day
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
                                Text(page.short.uppercased())
                                    .font(.cavnarBody(11, weight: 700))
                                    .tracking(0.8)
                                Text(page.dateLabel)
                                    .font(.cavnarNumber(14, weight: 700))
                                Group {
                                    if page.notWritten {
                                        Image(systemName: "exclamationmark.triangle.fill")
                                            .font(.system(size: 9, weight: .bold))
                                            .foregroundStyle(Color.cavnarAmber)
                                    } else {
                                        Text("\(page.count)")
                                            .font(.cavnarNumber(11, weight: 600))
                                    }
                                }
                                .opacity(on ? 1 : 0.75)
                            }
                            .foregroundStyle(on ? Color.cavnarInk : Color.cavnarInk2)
                            .frame(width: 54, height: 62)
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
                                            + (page.notWritten ? "not written" : "\(page.count) shifts"))
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
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
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
                            Text(p.name).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                            if !p.roles.isEmpty {
                                Text(p.roles.joined(separator: ", "))
                                    .font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        Spacer(minLength: 8)
                        VStack(alignment: .trailing, spacing: 2) {
                            (Text(ScheduleWeekMath.hoursText(p.hours)).font(.cavnarNumber(15, weight: 700))
                             + Text("h").font(.cavnarNumber(12, weight: 600)))
                                .foregroundStyle(p.hours > 40 ? Color.cavnarRed : Color.cavnarInk)
                            Text("\(p.days) \(p.days == 1 ? "day" : "days")")
                                .font(.cavnarNumber(12)).foregroundStyle(Color.cavnarInk3)
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

/// One person's week: each shift by day, their hours and days on.
struct PersonWeekSheet: View {
    let person: PersonWeek
    var onPick: ((ScheduleRow) -> Void)? = nil
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack(spacing: 18) {
                        stat(ScheduleWeekMath.hoursText(person.hours) + "h", "this week",
                             tone: person.hours > 40 ? .cavnarRed : .cavnarInk)
                        stat("\(person.days)", person.days == 1 ? "day on" : "days on", tone: .cavnarInk)
                        stat("\(person.rows.count)", person.rows.count == 1 ? "shift" : "shifts", tone: .cavnarInk)
                    }
                    if person.hours > 40 {
                        HomeMixedText.make("Past 40h \u{2014} overtime at 1.5\u{00D7} on the hours over.",
                                           size: 13.5, weight: 600, color: .cavnarRed)
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
                                            .font(.cavnarBody(14.5, weight: 700))
                                            .foregroundStyle(Color.cavnarInk)
                                        HomeMixedText.make([row.date.map(CavnarDate.mdy), row.role].compactMap { $0 }
                                                            .filter { !$0.isEmpty }.joined(separator: " \u{00B7} "),
                                                           size: 13, color: .cavnarInk3)
                                    }
                                    Spacer(minLength: 8)
                                    VStack(alignment: .trailing, spacing: 2) {
                                        Text("\(row.shiftStart ?? "")\u{2013}\(row.shiftEnd ?? "")")
                                            .font(.cavnarNumber(14.5, weight: 600))
                                            .foregroundStyle(Color.cavnarInk2)
                                        Text(ScheduleWeekMath.hoursText(ScheduleWeekMath.hours(of: row)) + "h")
                                            .font(.cavnarNumber(12.5))
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
                            .font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome(person.name)
        }
        .presentationDetents([.medium, .large])
    }

    private func stat(_ value: String, _ label: String, tone: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.cavnarNumber(24, weight: 700)).foregroundStyle(tone)
            Text(label).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
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

    static func tiles(_ r: GeneratedSchedule) -> [Tile] {
        var out: [Tile] = []
        let q = r.quality
        let score = q?.checked == true ? q?.score : nil
        let band = (q?.band ?? "").isEmpty ? nil : q?.band
        out.append(Tile(key: "quality", label: "Shift quality", value: score.map(String.init), unit: "",
                        sub: band.map { $0.prefix(1).uppercased() + $0.dropFirst() + " \u{00B7} graded from the finished week, out of 100" }
                            ?? "Graded from the finished week",
                        tone: score.map { $0 >= 80 ? .good : ($0 >= 60 ? .warn : .bad) } ?? .neutral))
        let lv = r.laborView
        let basis = lv.map { $0.isAllIn ? "all-in" : "hourly pay only" } ?? ""
        if let pct = lv?.pct {
            var sub = "of forecast sales, \(basis)"
            if let t = lv?.targetPct {
                sub += (lv?.targetBasis == "hourly" ? " \u{00B7} the hourly budget is " : " \u{00B7} your target is ")
                    + "\(ScheduleBuildSettings.pct(t))%"
            }
            let over = (lv?.targetPct).map { pct > $0 + 0.5 } ?? false
            out.append(Tile(key: "labor", label: "Labor", value: String(format: "%.1f", pct), unit: "%", sub: sub,
                            tone: over ? .bad : .good))
        } else {
            out.append(Tile(key: "labor", label: "Labor", value: nil, unit: "",
                            sub: "Needs the forecast this draft was built on \u{2014} generate a new draft to see it",
                            tone: .neutral))
        }
        let cov = q?.dimensions?.first { $0.key == "coverage" }?.score
        out.append(Tile(key: "coverage", label: "Coverage", value: cov.map(String.init), unit: cov == nil ? "" : "%",
                        sub: "of the people each shift needs, on it",
                        tone: cov.map { $0 >= 90 ? .good : ($0 >= 75 ? .warn : .bad) } ?? .neutral))
        if let pc = r.projectedCost {
            let ot = pc.overtimeHours ?? 0
            out.append(Tile(key: "overtime", label: "Overtime", value: ScheduleWeekMath.hoursText(ot), unit: "h",
                            sub: ot > 0 ? "\(money(pc.overtimePremium ?? 0)) in premium pay" : "Nobody past 40h",
                            tone: ot > 0 ? .bad : .good))
        } else {
            out.append(Tile(key: "overtime", label: "Overtime", value: nil, unit: "",
                            sub: "Priced once the draft is saved", tone: .neutral))
        }
        let hard = r.review?.hardCount ?? 0, soft = r.review?.softCount ?? 0
        out.append(Tile(key: "warnings", label: "Warnings", value: String(hard + soft), unit: "",
                        sub: hard > 0 ? "\(hard) break a hard rule \u{00B7} fix before sending"
                            : (soft > 0 ? "Soft rules only \u{00B7} worth a look" : "Every rule kept"),
                        tone: hard > 0 ? .bad : (soft > 0 ? .warn : .good)))
        if let sav = lv?.savings, let rec = lv?.recentPct, let rev = r.forecastSales, rev > 0 {
            out.append(Tile(key: "savings", label: "Under your recent labor", value: money(sav), unit: "",
                            sub: "this week vs your last \(lv?.recentDays ?? 14) days at \(ScheduleBuildSettings.pct(rec))% labor (\(basis)), on \(money(rev)) forecast sales",
                            tone: .hero))
        } else {
            out.append(Tile(key: "savings", label: "Under your recent labor", value: nil, unit: "",
                            sub: "Needs a measured labor % for your recent weeks and this draft\u{2019}s forecast",
                            tone: .hero))
        }
        return out
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
                            Text("SCHEDULE COMPLETE")
                                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                                .tracking(1.4)
                                .foregroundStyle(Color.cavnarEmber)
                            Text("The week is built")
                                .font(.cavnarHeadline(26))
                                .foregroundStyle(Color.cavnarInk)
                            HomeMixedText.make(ScheduleSummaryTiles.headline(result), size: 13.5, color: .cavnarInk3)
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
                        .font(.cavnarBody(12.5))
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
            Text(tile.label.uppercased())
                .font(.cavnarBody(10.5, weight: 700))
                .tracking(1)
                .foregroundStyle(Color.cavnarEmber2)
            HStack(alignment: .firstTextBaseline, spacing: 1) {
                Text(tile.value ?? "\u{2014}")
                    .font(.cavnarNumber(26, weight: 700))
                    .foregroundStyle(tile.value == nil ? Color.cavnarInk3 : (tile.tone == .neutral ? Color.cavnarInk : tone))
                if tile.value != nil, !tile.unit.isEmpty {
                    Text(tile.unit).font(.cavnarNumber(14, weight: 600)).foregroundStyle(Color.cavnarInk3)
                }
            }
            HomeMixedText.make(tile.sub, size: 12, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if tile.key == "savings" {
                Text("PROJECTION \u{00B7} NOT YET EARNED")
                    .font(.cavnarBody(9.5, weight: 700))
                    .tracking(0.6)
                    .foregroundStyle(Color.cavnarEmber2)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Color.cavnarEmber.opacity(0.12)))
            }
            if showsGo {
                HStack(spacing: 3) {
                    Text("See them").font(.cavnarBody(12.5, weight: 700))
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
                Text("SCHEDULE").font(.cavnarBody(10, weight: 700)).tracking(1.4).foregroundStyle(Color.cavnarEmber)
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
