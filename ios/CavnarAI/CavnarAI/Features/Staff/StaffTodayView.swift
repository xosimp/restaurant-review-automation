import SwiftUI

/// Today — the staff app's first tab and its daily companion (employee
/// audit V1, UX-09/10/16, C6/C7, H6). Top to bottom:
///   a small header ("Hi, Jordan", the restaurant, the inbox)
///   the hero: the next shift, EVERY leg of a double, role · station ·
///     section · hours, notes, the break, "Starts in 2h 15m", and the
///     one-tap row (Running late · Give up / Swap · Checklist · Message)
///   How did your shift go? (only after a finished shift due a rating)
///   Waiting on you (only when a swap or an offer waits)
///   hours this week, the overtime heads-up, last shift's tips
///   Before service (only on a working day)
///   who's on with me
///   a guest named you (only when one did)
///   the week, then later shifts
///   "Updated 3:42pm"
/// Ember is spent once: the hero's ambient light. Every section that can
/// fail says so in place with Try again.
struct StaffTodayView: View {
    let store: StaffPortalStore
    @Environment(StaffSessionStore.self) private var staff

    @State private var changing: ShiftChange?
    @State private var lateLeg: LateTarget?
    @State private var clock = CavnarEntranceClock()
    @State private var now = Date()

    struct ShiftChange: Identifiable {
        let day: StaffWeekDay
        let mode: StaffShiftChangeSheet.Mode
        var id: String { day.date + "|" + (day.shift?.shiftStart ?? "") + (mode == .swap ? "|swap" : "|drop") }
    }

    struct LateTarget: Identifiable {
        let date: String
        let leg: StaffShift
        var id: String { date + "|" + (leg.shiftStart ?? "") }
    }

    var body: some View {
        StaffTabScroll(refresh: { await store.refreshAll() }) {
            header
            content
            stamp
        }
        // The relative line ("Starts in 2h 15m") moves with the clock.
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(30))
                now = Date()
            }
        }
        .sheet(item: $changing, onDismiss: {
            Task {
                await store.reloadShifts()
                await store.reloadBadges()
            }
        }) { change in
            // I3's sheet, by its existing initializer: the day narrowed to
            // the one leg chosen, so either half of a double can move (C6).
            StaffShiftChangeSheet(day: change.day, mode: change.mode)
        }
        .sheet(item: $lateLeg) { target in
            StaffRunningLateSheet(store: store, date: target.date, leg: target.leg)
        }
    }

    // MARK: Header

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                if let restaurant = store.profile.value?.restaurant, !restaurant.isEmpty {
                    StaffKicker(text: restaurant)
                }
                Text(greeting)
                    .font(.cavnarBody(CavnarType.emphasis, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .accessibilityAddTraits(.isHeader)
            }
            Spacer(minLength: 0)
            inboxButton
        }
        .padding(.top, 6)
    }

    private var greeting: String {
        guard let name = store.profile.value?.name,
              let first = name.split(separator: " ").first else { return "Today" }
        return "Hi, \(first)"
    }

    private var inboxButton: some View {
        Button {
            store.showingInbox = true
        } label: {
            ZStack(alignment: .topTrailing) {
                Image(systemName: "tray")
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(Color.cavnarInk2)
                    .frame(width: 44, height: 44)
                if store.inboxBadge > 0 {
                    Text(store.inboxBadge > 99 ? "99+" : "\(store.inboxBadge)")
                        .font(.cavnarNumber(11, weight: 700))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 5)
                        .frame(minWidth: 18, minHeight: 18)
                        .background(Capsule().fill(Color.cavnarRed))
                        .offset(x: 2, y: 2)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Inbox")
        .accessibilityValue(store.inboxBadge > 0 ? "\(store.inboxBadge) unread" : "Nothing unread")
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        switch store.shifts.phase {
        case .loading:
            StaffLoadingLine(text: "Loading your shifts")
                .padding(.vertical, 8)
        case .failed:
            StaffLoadFailed(what: "your shifts", message: store.shifts.error) {
                await store.reloadShifts()
            }
            .padding(16)
            .cavnarCard()
            // The portal didn't load: leaving must not depend on Me
            // loading either (a shared phone handed on mid-outage).
            StaffTextButton(title: "Sign out", tone: .cavnarRed) { staff.signOut() }
                .accessibilityHint("Signs you out of this phone. The next person signs in with their PIN.")
        case .ready:
            if let shifts = store.shifts.value {
                ready(shifts)
            }
        }
    }

    @ViewBuilder
    private func ready(_ shifts: StaffShiftsResponse) -> some View {
        if shifts.published == false {
            CavnarEmptyHearth(title: "No schedule posted yet",
                              message: "It shows up here the moment your manager publishes one.")
                .cavnarCard()
        } else {
            let week = shifts.week ?? []
            let hero = StaffTodayPlan.hero(week: week, now: now)
            heroCard(hero, shifts: shifts)
                .staffRise(0, clock)
            // "How did your shift go?" — only after a finished shift the
            // server says is due a rating (V6); otherwise it draws nothing.
            StaffPulseCard(store: store)
            if let sentence = StaffWaiting.sentence(store.waiting.value) {
                StaffWaitingStrip(sentence: sentence) { store.selectedTab = .requests }
                    .staffRise(1, clock)
            }
            StaffStatsTiles(store: store, shifts: shifts)
                .staffRise(2, clock)
            StaffBriefCard(section: store.brief, reload: { await store.reloadBrief() })
            if let hero {
                StaffCoworkersCard(section: store.coworkers, hero: hero) {
                    await store.reloadCoworkers()
                }
            }
            StaffRecognitionCard(section: store.recognition)
            weekList(week)
            laterList(StaffTodayPlan.later(upcoming: shifts.upcoming ?? [], week: week))
        }
    }

    // MARK: Hero

    @ViewBuilder
    private func heroCard(_ hero: StaffHero?, shifts: StaffShiftsResponse) -> some View {
        if let hero {
            StaffHeroCard(hero: hero,
                          lateReport: { leg in store.lateReport(date: hero.day.date, shiftStart: leg.shiftStart) },
                          onLate: { lateLeg = LateTarget(date: hero.day.date, leg: hero.nextLeg) },
                          onChange: { leg, mode in changing = ShiftChange(day: hero.day.focused(on: leg), mode: mode) },
                          onChecklist: { store.selectedTab = .tasks },
                          onMessage: {
                              store.messageShiftDate = hero.day.date
                              store.showingMessages = true
                          })
        } else {
            // Off for the whole week (UX-09): said in the hero's own place.
            VStack(alignment: .leading, spacing: 8) {
                StaffKicker(text: "Next shift")
                Text("You\u{2019}re off for the next 7 days")
                    .font(.cavnarHeadline(CavnarType.section))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if let next = StaffTodayPlan.nextBeyondWeek(upcoming: shifts.upcoming ?? [], week: shifts.week ?? []) {
                    HomeMixedText.make(next, size: CavnarType.body, color: .cavnarInk2)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard(.hero)
            .accessibilityElement(children: .combine)
        }
    }

    // MARK: The week

    @ViewBuilder
    private func weekList(_ week: [StaffWeekDay]) -> some View {
        if !week.isEmpty {
            HStack(alignment: .firstTextBaseline) {
                StaffKicker(text: "The next 7 days")
                Spacer()
                if let h = store.shifts.value?.weekHours, let label = StaffTime.hoursLabel(h) {
                    HomeMixedText.make(label, size: CavnarType.caption, weight: 600, color: .cavnarInk3)
                }
            }
            .padding(.top, 8)
            ForEach(Array(week.enumerated()), id: \.element.id) { index, day in
                StaffDayRow(day: day,
                            onChange: { leg, mode in changing = ShiftChange(day: day.focused(on: leg), mode: mode) })
                    .staffRise(index + 3, clock)
            }
        }
    }

    @ViewBuilder
    private func laterList(_ later: [(date: String, legs: [StaffShift])]) -> some View {
        if !later.isEmpty {
            StaffKicker(text: "Later")
                .padding(.top, 8)
            ForEach(later, id: \.date) { item in
                StaffLaterRow(date: item.date, legs: item.legs)
            }
        }
    }

    // MARK: Freshness

    @ViewBuilder
    private var stamp: some View {
        let cached = store.shifts.fromCache
        if let line = StaffFreshness.stamp(at: cached ? store.shifts.loadedAt : (store.lastRefresh ?? store.shifts.loadedAt),
                                           fromCache: cached, now: now) {
            VStack(spacing: 4) {
                HomeMixedText.make(line, size: CavnarType.caption, weight: cached ? 600 : 400,
                                   color: cached ? .cavnarAmber : .cavnarInk3)
                if store.shifts.isStale, let error = store.shifts.error {
                    Text("Couldn\u{2019}t refresh: \(error)")
                        .font(.cavnarBody(CavnarType.caption))
                        .foregroundStyle(Color.cavnarRed)
                        .multilineTextAlignment(.center)
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 10)
            .accessibilityElement(children: .combine)
        }
    }
}

// MARK: - The hero card

/// The next shift as the screen's one hero (`.cavnarCard(.hero)`, UX-09):
/// kicker, "Tonight" in Clash, each leg's time in the number face at the
/// card size, what the leg is, its break and note, its request state, the
/// relative line, and the one-tap row.
struct StaffHeroCard: View {
    let hero: StaffHero
    let lateReport: (StaffShift) -> StaffRunningLateReport?
    let onLate: () -> Void
    let onChange: (StaffShift, StaffShiftChangeSheet.Mode) -> Void
    let onChecklist: () -> Void
    let onMessage: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                StaffKicker(text: "Next shift")
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) { title; date }
                    VStack(alignment: .leading, spacing: 2) { title; date }
                }
            }
            ForEach(Array(hero.day.legs.enumerated()), id: \.offset) { _, leg in
                StaffLegBlock(leg: leg, large: true, late: lateReport(leg))
            }
            HomeMixedText.make(hero.relative, size: CavnarType.body, weight: 700, color: .cavnarInk)
                .accessibilityLabel(hero.relative)
            actions
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.hero)
        .accessibilityElement(children: .contain)
    }

    private var title: some View {
        Text(hero.title)
            .font(.cavnarHeadline(CavnarType.section))
            .foregroundStyle(Color.cavnarInk)
            .accessibilityAddTraits(.isHeader)
    }

    private var date: some View {
        HomeMixedText.make("\(hero.day.weekday) \(CavnarDate.mdy(hero.day.date))", size: CavnarType.secondary,
                           color: .cavnarInk3)
    }

    // MARK: One-tap row

    private var changeable: [StaffShift] { hero.day.legs.filter { $0.canDrop || $0.canSwap } }

    enum Action: Hashable { case late, change, checklist, message }

    /// Running late only for today's shift before it ends (H1); Give up /
    /// Swap only while a leg can still change hands; Checklist only today.
    var actionList: [Action] {
        var out: [Action] = []
        if hero.canRunLate { out.append(.late) }
        if !changeable.isEmpty { out.append(.change) }
        if hero.day.isToday { out.append(.checklist) }
        out.append(.message)
        return out
    }

    @ViewBuilder
    private var actions: some View {
        let list = actionList
        // Two across, one across when the text is large (UX-20).
        ViewThatFits(in: .horizontal) {
            Grid(horizontalSpacing: 8, verticalSpacing: 8) {
                ForEach(Array(stride(from: 0, to: list.count, by: 2)), id: \.self) { i in
                    GridRow {
                        button(list[i])
                        if i + 1 < list.count {
                            button(list[i + 1])
                        } else {
                            Color.clear.frame(height: 1)
                        }
                    }
                }
            }
            VStack(spacing: 8) {
                ForEach(list, id: \.self) { button($0) }
            }
        }
        .padding(.top, 2)
    }

    @ViewBuilder
    private func button(_ action: Action) -> some View {
        switch action {
        case .late: quiet("Running late", "clock.badge.exclamationmark", action: onLate)
        case .change: changeMenu
        case .checklist: quiet("Checklist", "checklist", action: onChecklist)
        case .message: quiet("Message", "bubble.left", action: onMessage)
        }
    }

    private func quiet(_ title: String, _ icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .labelStyle(.titleAndIcon)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
                .frame(maxWidth: .infinity, minHeight: 20)
        }
        .buttonStyle(StaffQuietButtonStyle())
    }

    /// Give up / Swap — per leg on a double, so the evening half can move
    /// on its own (C6). Opens I3's StaffShiftChangeSheet.
    private var changeMenu: some View {
        Menu {
            ForEach(Array(changeable.enumerated()), id: \.offset) { _, leg in
                let suffix = changeable.count > 1 ? " \(leg.timeRange)" : ""
                if leg.canDrop {
                    Button("Give up\(suffix)") { onChange(leg, .drop) }
                }
                if leg.canSwap {
                    Button("Swap\(suffix)") { onChange(leg, .swap) }
                }
            }
        } label: {
            Label("Give up / Swap", systemImage: "arrow.left.arrow.right")
                .labelStyle(.titleAndIcon)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
                .frame(maxWidth: .infinity, minHeight: 20)
                .modifier(StaffQuietSurface())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Give up or swap this shift")
    }
}

/// One leg: its time in the number face, what it is, the break, the note,
/// and the live request on it. `large` is the hero's size (cardNumber);
/// the week rows use the body size.
struct StaffLegBlock: View {
    let leg: StaffShift
    var large: Bool = false
    var late: StaffRunningLateReport? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(leg.timeRange.isEmpty ? "Time to be set" : leg.timeRange)
                .font(leg.timeRange.isEmpty ? .cavnarBody(CavnarType.body)
                                            : .cavnarNumber(large ? CavnarType.cardNumber : 17, weight: 600))
                .foregroundStyle(leg.timeRange.isEmpty ? Color.cavnarInk3 : Color.cavnarInk)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            if !leg.detailLine.isEmpty {
                HomeMixedText.make(leg.detailLine, size: large ? CavnarType.body : CavnarType.secondary,
                                   color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let line = leg.breakLine {
                HomeMixedText.make(line, size: CavnarType.secondary, color: .cavnarInk3)
            }
            if let note = leg.noteLine {
                HomeMixedText.make("Note: \(note)", size: CavnarType.secondary, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let request = leg.request {
                StaffRequestChip(request: request, showDetail: large)
            }
            if let late {
                HomeMixedText.make("You told your manager about \(late.etaMinutes) min late.",
                                   size: CavnarType.secondary, weight: 600, color: .cavnarAmber)
            }
        }
    }
}

/// "Giving up" / "Swap" / "Open" — neutral ink, amber while it is open and
/// still theirs (DS §9: no verdict is neutral, never ember).
struct StaffRequestChip: View {
    let request: StaffShiftRequestState
    var showDetail: Bool = false

    var body: some View {
        let tone: CavnarTone = request.isWatch ? .warning : .neutral
        VStack(alignment: .leading, spacing: 3) {
            Text(request.chipLabel)
                .font(.cavnarBody(CavnarType.caption, weight: 700))
                .foregroundStyle(tone.foreground)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(Capsule().fill(tone.background))
            if showDetail {
                Text(request.detail)
                    .font(.cavnarBody(CavnarType.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(request.detail)
    }
}

// MARK: - Day rows

/// One day of the week: every leg, its hours, notes, section and request
/// state; "Not posted yet" for a day no published week covers; today
/// labelled (a kicker and a brighter row), never coloured green (UX-16).
/// One VoiceOver stop per day (UX-18); the change menu is its own.
struct StaffDayRow: View {
    let day: StaffWeekDay
    let onChange: (StaffShift, StaffShiftChangeSheet.Mode) -> Void

    private var changeable: [StaffShift] { day.legs.filter { $0.canDrop || $0.canSwap } }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(day.weekday)
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    HomeMixedText.make(CavnarDate.mdy(day.date), size: CavnarType.caption, color: .cavnarInk3)
                    if day.isToday {
                        Text("TODAY")
                            .font(.cavnarBody(CavnarType.kicker, weight: 700))
                            .kerning(1.2)
                            .foregroundStyle(Color.cavnarInk)
                    }
                }
                if day.legs.isEmpty {
                    Text(day.isPosted ? "Off" : "Not posted yet")
                        .font(.cavnarBody(CavnarType.secondary))
                        .foregroundStyle(Color.cavnarInk3)
                } else {
                    ForEach(Array(day.legs.enumerated()), id: \.offset) { _, leg in
                        StaffLegBlock(leg: leg)
                    }
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(StaffTodayPlan.accessibilityLabel(day))
            Spacer(minLength: 0)
            if !changeable.isEmpty {
                Menu {
                    ForEach(Array(changeable.enumerated()), id: \.offset) { _, leg in
                        let suffix = changeable.count > 1 ? " \(leg.timeRange)" : ""
                        if leg.canDrop { Button("Can\u{2019}t work this\(suffix)") { onChange(leg, .drop) } }
                        if leg.canSwap { Button("Swap with a colleague\(suffix)") { onChange(leg, .swap) } }
                    }
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .accessibilityLabel("Change this shift")
            }
        }
        .padding(.vertical, 12)
        .padding(.leading, 14)
        .padding(.trailing, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(day.isToday ? Color.cavnarPaper3.opacity(0.55) : Color.cavnarPaper2.opacity(0.6),
                    in: RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .strokeBorder(day.isToday ? Color.cavnarInk.opacity(0.18) : Color.cavnarPaper3.opacity(0.5),
                              lineWidth: 1)
        )
        .opacity(day.legs.isEmpty ? 0.75 : 1)
    }
}

/// A shift past the seven days (`upcoming`, UX-10).
struct StaffLaterRow: View {
    let date: String
    let legs: [StaffShift]

    var body: some View {
        let times = legs.map(\.timeRange).filter { !$0.isEmpty }.joined(separator: " \u{00B7} ")
        let role = legs.compactMap(\.role).first(where: { !$0.isEmpty })
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(legs.first?.day ?? "")
                    .font(.cavnarBody(CavnarType.body, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                HomeMixedText.make(CavnarDate.mdy(date), size: CavnarType.caption, color: .cavnarInk3)
            }
            if !times.isEmpty {
                Text(times)
                    .font(.cavnarNumber(15, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
            }
            if let role {
                Text(role).font(.cavnarBody(CavnarType.secondary)).foregroundStyle(Color.cavnarInk2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 10)
        .padding(.horizontal, 14)
        .background(Color.cavnarPaper2.opacity(0.6),
                    in: RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))
        .accessibilityElement(children: .combine)
    }
}

// MARK: - The quiet action surface

/// The hero's one-tap actions: the quiet secondary look (white 5% surface,
/// 1pt ink hairline at 16%, ink text — CavnarSecondaryButtonStyle's)
/// sized for a 2×2 row, at least 44pt tall. Ember stays on the hero.
struct StaffQuietButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .modifier(StaffQuietSurface(pressed: configuration.isPressed))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

struct StaffQuietSurface: ViewModifier {
    var pressed = false

    func body(content: Content) -> some View {
        content
            .font(.cavnarBody(CavnarType.secondary, weight: 700))
            .foregroundStyle(Color.cavnarInk)
            .padding(.horizontal, 10)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .fill(Color.white.opacity(pressed ? 0.09 : 0.05)))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .strokeBorder(Color.cavnarInk.opacity(0.16), lineWidth: 1))
            .contentShape(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
    }
}
