import SwiftUI

/// Today — the staff app's first tab and its daily companion (employee
/// audit V1, UX-09/10/16, C6/C7, H6; iOS readability round 10/8/26). Top to
/// bottom, most urgent first:
///   a small header ("Hi, Jordan", the restaurant, the inbox), and under it
///     an amber "As of 3:42pm · offline" while the week is the phone's copy
///   the hero: the next shift, EVERY leg of a double, when it starts, and
///     ONE primary for where the person is in the day — Tasks · 3/12 on
///     shift or within 30 minutes of it, Running late in the hour before a
///     shift today — with the rest (Running late, until 15 minutes in ·
///     Change shift · Tasks · Message) quiet in one row
///   From your manager (only an urgent note not yet "Got it", or a reply)
///   Waiting on you (only when a swap or an offer waits)
///   Before service (only on a working day)
///   who's on with me (the first four, overlapping hours first)
///   How did your shift go? (only after a finished shift due a rating)
///   hours this week, the overtime heads-up, last shift's tips
///   a guest named you (only when one did)
///   the week, one line a day (tap a day for its break, notes and request),
///     then later shifts
///   "Updated 3:42pm"
/// Ember is spent once: the hero. Every section that can fail says so in
/// place with Try again.
struct StaffTodayView: View {
    let store: StaffPortalStore
    @Environment(StaffSessionStore.self) private var staff

    @State private var changing: ShiftChange?
    @State private var lateLeg: LateTarget?
    @State private var clock = CavnarEntranceClock()
    @State private var now = Date()
    private let tasks = StaffTasksStore.shared
    private let network = NetworkMonitor.shared

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
            offlineWarning
            content
            stamp
        }
        // The relative line ("Starts in 2h 15m") and the hero's primary
        // move with the clock.
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
        HStack(alignment: .center, spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                if let restaurant = store.profile.value?.restaurant, !restaurant.isEmpty {
                    CavnarKicker(restaurant)
                }
                Text(greeting)
                    .cavnarText(.headline)
                    .accessibilityAddTraits(.isHeader)
            }
            Spacer(minLength: 0)
            inboxButton
        }
        .padding(.top, CavnarSpace.xxs)
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
                    .font(.cavnar(.lead).weight(.semibold))
                    .foregroundStyle(Color.cavnarInk2)
                    .frame(width: 44, height: 44)
                if store.inboxBadge > 0 {
                    Text(store.inboxBadge > 99 ? "99+" : "\(store.inboxBadge)")
                        .font(.cavnarNumber(CavnarType.tag, weight: 700))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 5)
                        .frame(minWidth: 18, minHeight: 18)
                        .background(Capsule().fill(Color.cavnarRed))
                        .offset(x: 2, y: 2)
                }
            }
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Inbox")
        .accessibilityValue(store.inboxBadge > 0 ? "\(store.inboxBadge) unread" : "Nothing unread")
    }

    // MARK: Offline / cached — said at the top, not only at the foot

    @ViewBuilder
    private var offlineWarning: some View {
        let s = store.shifts
        let offline = !network.isOnline
        if s.value != nil, s.isStale || (s.fromCache && (offline || s.error != nil)),
           let line = StaffFreshness.warning(at: s.loadedAt, offline: offline, now: now) {
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    Image(systemName: offline ? "wifi.slash" : "exclamationmark.arrow.circlepath")
                        .font(.cavnar(.secondary).weight(.semibold))
                        .foregroundStyle(Color.cavnarAmber)
                        .accessibilityHidden(true)
                    CavnarMixedText(line, role: .secondary, color: .cavnarAmber)
                }
                if !offline, let error = s.error {
                    Text(error)
                        .cavnarText(.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, CavnarSpace.s)
            .padding(.vertical, CavnarSpace.xs)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.cavnarAmberBg, in: RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
            .accessibilityElement(children: .combine)
        }
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        switch store.shifts.phase {
        case .loading:
            StaffLoadingLine(text: "Loading your shifts")
                .padding(.vertical, CavnarSpace.xs)
            strips
        case .failed:
            // What a manager or a teammate is waiting on doesn't depend on
            // the week loading (re-audit L10).
            strips
            StaffLoadFailed(what: "your shifts", message: store.shifts.error) {
                await store.reloadShifts()
            }
            .cavnarCard()
            // The portal didn't load: leaving must not depend on Me
            // loading either (a shared phone handed on mid-outage).
            StaffTextButton(title: "Sign out", tone: .cavnarRedText) { staff.signOut() }
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
            strips
            CavnarEmptyHearth(title: "No schedule posted yet",
                              message: "It shows up here the moment your manager publishes one.")
                .cavnarCard()
        } else {
            let week = shifts.week ?? []
            let hero = StaffTodayPlan.hero(week: week, now: now)
            heroCard(hero, shifts: shifts)
                .staffRise(0, clock)
            strips
            StaffBriefCard(section: store.brief, focusLabel: StaffBriefCard.focusLabel(hero),
                           reload: { await store.reloadBrief() })
            if let hero {
                StaffCoworkersCard(section: store.coworkers, hero: hero) {
                    await store.reloadCoworkers()
                }
            }
            // "How did your shift go?" — only after a finished shift the
            // server says is due a rating (V6); otherwise it draws nothing.
            StaffPulseCard(store: store)
            StaffStatsTiles(store: store, shifts: shifts)
                .staffRise(2, clock)
            StaffRecognitionCard(section: store.recognition)
            weekList(week, heroDate: hero?.day.date)
                .staffRise(3, clock)
            laterList(StaffTodayPlan.later(upcoming: shifts.upcoming ?? [], week: week))
        }
    }

    /// Urgent and unread from a manager, then what a teammate is waiting
    /// on — right under the hero, or at the top when there is no hero card
    /// (no published week, or the week didn't load: re-audit L10). An
    /// ordinary announcement stays behind the tray.
    @ViewBuilder
    private var strips: some View {
        if let strip = StaffManagerStrip.content(store.inbox.value) {
            StaffManagerStripView(text: strip.text, urgent: strip.urgent) {
                if strip.opensThread {
                    store.messageShiftDate = nil
                    store.showingMessages = true
                } else {
                    store.showingInbox = true
                }
            }
            .staffRise(1, clock)
        }
        if let sentence = StaffWaiting.sentence(store.waiting.value) {
            StaffWaitingStrip(sentence: sentence, lead: StaffWaiting.lead(store.waiting.value)) {
                store.selectedTab = .requests
            }
            .staffRise(1, clock)
        }
    }

    // MARK: Hero

    @ViewBuilder
    private func heroCard(_ hero: StaffHero?, shifts: StaffShiftsResponse) -> some View {
        if let hero {
            let progress = tasks.progress
            StaffHeroCard(hero: hero,
                          primary: StaffTodayPlan.heroPrimary(hero, now: now, hasTasks: progress != nil),
                          offersLate: StaffTodayPlan.offersRunningLate(hero, now: now),
                          tasks: progress,
                          lateReport: { leg in store.lateReport(date: hero.day.date, shiftStart: leg.shiftStart) },
                          onLate: { lateLeg = LateTarget(date: hero.day.date, leg: hero.nextLeg) },
                          onChange: { leg, mode in changing = ShiftChange(day: hero.day.focused(on: leg), mode: mode) },
                          onTasks: { store.selectedTab = .tasks },
                          onMessage: {
                              store.messageShiftDate = hero.day.date
                              store.showingMessages = true
                          })
        } else {
            // Off for the whole week (UX-09): said in the hero's own place.
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Next shift")
                Text("You\u{2019}re off for the next 7 days")
                    .cavnarText(.headline)
                    .fixedSize(horizontal: false, vertical: true)
                if let next = StaffTodayPlan.nextBeyondWeek(upcoming: shifts.upcoming ?? [], week: shifts.week ?? []) {
                    CavnarMixedText(next, role: .body)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard(.hero)
            .accessibilityElement(children: .combine)
        }
    }

    // MARK: The week

    /// One card, one line a day. The hero's day points up at the hero
    /// instead of repeating it; a tap opens a day's break, notes and
    /// request.
    @ViewBuilder
    private func weekList(_ week: [StaffWeekDay], heroDate: String?) -> some View {
        if !week.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("The next 7 days")
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(week.enumerated()), id: \.element.id) { index, day in
                        if index > 0 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                        }
                        StaffDayRow(day: day, isHeroDay: day.date == heroDate,
                                    onChange: { leg, mode in changing = ShiftChange(day: day.focused(on: leg), mode: mode) })
                    }
                }
                .padding(.horizontal, CavnarSpace.m)
                .padding(.vertical, CavnarSpace.xxs)
                .background(Color.cavnarPaper2.opacity(0.6),
                            in: RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))
            }
            .padding(.top, CavnarSpace.xs)
        }
    }

    @ViewBuilder
    private func laterList(_ later: [(date: String, legs: [StaffShift])]) -> some View {
        if !later.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Later")
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(later.enumerated()), id: \.element.date) { index, item in
                        if index > 0 {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                        }
                        StaffLaterRow(date: item.date, legs: item.legs)
                    }
                }
                .padding(.horizontal, CavnarSpace.m)
                .padding(.vertical, CavnarSpace.xxs)
                .background(Color.cavnarPaper2.opacity(0.6),
                            in: RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))
            }
            .padding(.top, CavnarSpace.xs)
        }
    }

    // MARK: Freshness

    /// "Updated 3:42pm" at the foot. A cached or failed refresh is said at
    /// the top (`offlineWarning`); here it is only the time.
    @ViewBuilder
    private var stamp: some View {
        let cached = store.shifts.fromCache
        if let line = StaffFreshness.stamp(at: cached ? store.shifts.loadedAt : (store.lastRefresh ?? store.shifts.loadedAt),
                                           fromCache: cached, now: now) {
            CavnarMixedText(line, role: .caption, color: .cavnarInk3)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.top, CavnarSpace.xs)
        }
    }
}

// MARK: - The hero card

/// The next shift as the screen's one hero (`.cavnarCard(.hero)`, UX-09):
/// kicker, "Tonight" in Clash, when it starts, each leg's time in the number
/// face, what the leg is, its break and note, its request state; then ONE
/// primary chosen by the moment (`StaffHeroPrimary`) and the rest quiet in
/// one row.
struct StaffHeroCard: View {
    let hero: StaffHero
    let primary: StaffHeroPrimary
    /// Running late is still offered (StaffTodayPlan.offersRunningLate):
    /// until 15 minutes into today's leg, then it leaves the quiet row.
    var offersLate: Bool = true
    let tasks: (done: Int, total: Int)?
    let lateReport: (StaffShift) -> StaffRunningLateReport?
    let onLate: () -> Void
    let onChange: (StaffShift, StaffShiftChangeSheet.Mode) -> Void
    let onTasks: () -> Void
    let onMessage: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                CavnarKicker("Next shift")
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) { title; date }
                    VStack(alignment: .leading, spacing: 2) { title; date }
                }
                if !hero.relative.isEmpty {
                    CavnarMixedText(hero.relative, role: .lead, color: .cavnarInk)
                        .accessibilityLabel(hero.relative)
                }
            }
            ForEach(Array(hero.day.legs.enumerated()), id: \.offset) { _, leg in
                StaffLegBlock(leg: leg, timeRole: hero.day.legs.count > 1 ? .figureM : .figureL,
                              late: lateReport(leg))
            }
            actions
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.hero)
        .accessibilityElement(children: .contain)
    }

    private var title: some View {
        Text(hero.title)
            .cavnarText(.headline)
            .accessibilityAddTraits(.isHeader)
    }

    private var date: some View {
        CavnarMixedText("\(hero.day.weekday) \(CavnarDate.mdy(hero.day.date))", role: .secondary)
    }

    // MARK: Actions

    private var changeable: [StaffShift] { hero.day.legs.filter { $0.canDrop || $0.canSwap } }

    enum Action: Hashable { case late, change, tasks, message }

    /// Running late only for today's shift before it ends (H1); Change
    /// shift only while a leg can still change hands; Tasks only today.
    var actionList: [Action] {
        var out: [Action] = []
        if hero.canRunLate && offersLate { out.append(.late) }
        if !changeable.isEmpty { out.append(.change) }
        if hero.day.isToday { out.append(.tasks) }
        out.append(.message)
        return out
    }

    /// Everything but the primary, quiet.
    var quietList: [Action] {
        switch primary {
        case .tasks: return actionList.filter { $0 != .tasks }
        case .late: return actionList.filter { $0 != .late }
        case .none: return actionList
        }
    }

    @ViewBuilder
    private var actions: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            switch primary {
            case .tasks: tasksPrimary
            case .late: latePrimary
            case .none: EmptyView()
            }
            quietRow
        }
        .padding(.top, 2)
    }

    private var tasksPrimary: some View {
        let done = tasks?.done ?? 0, total = tasks?.total ?? 0
        let fraction = total > 0 ? min(1, CGFloat(done) / CGFloat(total)) : 0
        return Button(action: onTasks) {
            VStack(spacing: CavnarSpace.xs) {
                HStack(spacing: CavnarSpace.xs) {
                    Image(systemName: "checklist").accessibilityHidden(true)
                    HomeMixedText.make("Tasks \u{00B7} \(done)/\(total)", role: .label, color: .white)
                }
                // The count's own progress, white on the ember.
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.white.opacity(0.28))
                        Capsule().fill(Color.white)
                            .frame(width: fraction > 0 ? max(6, geo.size.width * fraction) : 0)
                    }
                }
                .frame(height: 4)
                .accessibilityHidden(true)
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
        .accessibilityLabel("Tasks, \(done) of \(total) done")
        .accessibilityHint("Opens your tasks")
    }

    private var latePrimary: some View {
        Button(action: onLate) {
            Label("Running late", systemImage: "clock.badge.exclamationmark")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
        .accessibilityHint("Tells your manager how late you'll be")
    }

    /// One row when it fits; two across, then one across, as the text grows
    /// (UX-20).
    @ViewBuilder
    private var quietRow: some View {
        let list = quietList
        if !list.isEmpty {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: CavnarSpace.xs) {
                    ForEach(list, id: \.self) { button($0, icon: false) }
                }
                Grid(horizontalSpacing: CavnarSpace.xs, verticalSpacing: CavnarSpace.xs) {
                    ForEach(Array(stride(from: 0, to: list.count, by: 2)), id: \.self) { i in
                        GridRow {
                            button(list[i], icon: true)
                            if i + 1 < list.count {
                                button(list[i + 1], icon: true)
                            } else {
                                Color.clear.frame(height: 1)
                            }
                        }
                    }
                }
                VStack(spacing: CavnarSpace.xs) {
                    ForEach(list, id: \.self) { button($0, icon: true) }
                }
            }
        }
    }

    @ViewBuilder
    private func button(_ action: Action, icon: Bool) -> some View {
        switch action {
        case .late: quiet("Running late", "clock.badge.exclamationmark", icon: icon, action: onLate)
        case .change: changeMenu(icon: icon)
        case .tasks: quiet("Tasks", "checklist", icon: icon, action: onTasks)
        case .message: quiet("Message", "bubble.left", icon: icon, action: onMessage)
        }
    }

    private func quiet(_ title: String, _ systemImage: String, icon: Bool,
                       action: @escaping () -> Void) -> some View {
        Button(action: action) {
            label(title, systemImage, icon: icon)
        }
        .buttonStyle(StaffQuietButtonStyle())
    }

    @ViewBuilder
    private func label(_ title: String, _ systemImage: String, icon: Bool) -> some View {
        Group {
            if icon {
                Label(title, systemImage: systemImage).labelStyle(.titleAndIcon)
            } else {
                Text(title)
            }
        }
        .lineLimit(1)
        .fixedSize(horizontal: !icon, vertical: false)
        .frame(maxWidth: .infinity, minHeight: 20)
    }

    /// Change shift — Give up shift or Swap shift, per leg on a double, so
    /// the evening half can move on its own (C6). Opens I3's
    /// StaffShiftChangeSheet.
    private func changeMenu(icon: Bool) -> some View {
        Menu {
            ForEach(Array(changeable.enumerated()), id: \.offset) { _, leg in
                let suffix = changeable.count > 1 ? " \(leg.timeRange)" : ""
                if leg.canDrop {
                    Button("Give up shift\(suffix)") { onChange(leg, .drop) }
                }
                if leg.canSwap {
                    Button("Swap shift\(suffix)") { onChange(leg, .swap) }
                }
            }
        } label: {
            label("Change shift", "arrow.left.arrow.right", icon: icon)
                .modifier(StaffQuietSurface())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Change shift")
        .accessibilityHint("Give up shift or swap shift")
    }
}

/// One leg: its time in the number face, what it is, the break, the note,
/// and the live request on it. `timeRole` is the hero's figure size; the
/// week's expanded rows use FigureS, or no time at all when the day's one
/// line already said it.
struct StaffLegBlock: View {
    let leg: StaffShift
    var timeRole: CavnarText? = .figureS
    var late: StaffRunningLateReport? = nil

    private var large: Bool { timeRole == .figureL || timeRole == .figureM }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            if let timeRole {
                if leg.timeRange.isEmpty {
                    Text("Time to be set").cavnarText(.body)
                } else {
                    Text(leg.timeRange)
                        .cavnarText(timeRole)
                        .lineLimit(2)
                        .minimumScaleFactor(0.85)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if !leg.detailLine.isEmpty {
                CavnarMixedText(leg.detailLine, role: large ? .body : .secondary)
            }
            if let line = leg.breakLine {
                CavnarMixedText(line, role: .secondary)
            }
            if let note = leg.noteLine {
                CavnarMixedText("Note: \(note)", role: .secondary)
            }
            if let request = leg.request {
                StaffRequestChip(request: request, showDetail: large)
            }
            if let late {
                CavnarMixedText("You told your manager about \(late.etaMinutes) min late.",
                                role: .secondary, color: .cavnarAmber)
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
                .cavnarText(.tag, color: tone.foreground)
                .padding(.horizontal, CavnarSpace.xs)
                .padding(.vertical, 3)
                .background(Capsule().fill(tone.background))
            if showDetail {
                Text(request.detail)
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(request.detail)
    }
}

// MARK: - Day rows

/// One day of the week as ONE line — "Wed 10/8/26 · 4pm – 10pm · Server" —
/// at full contrast whether it is a shift, "Off" or "Not posted yet". The
/// hero's day points up at the hero. A tap opens the day's station, hours,
/// break, note and request; the ⋯ menu (Give up shift · Swap shift) is its
/// own control. One VoiceOver stop per day with the whole day in it (UX-18).
struct StaffDayRow: View {
    let day: StaffWeekDay
    var isHeroDay: Bool = false
    let onChange: (StaffShift, StaffShiftChangeSheet.Mode) -> Void

    @State private var expanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var changeable: [StaffShift] { day.legs.filter { $0.canDrop || $0.canSwap } }
    private var canExpand: Bool { !isHeroDay && StaffTodayPlan.dayHasDetail(day) }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .center, spacing: CavnarSpace.xs) {
                Button {
                    guard canExpand else { return }
                    Haptic.light()
                    if reduceMotion { expanded.toggle() } else {
                        withAnimation(.easeOut(duration: 0.22)) { expanded.toggle() }
                    }
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        line
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        if canExpand {
                            Image(systemName: "chevron.down")
                                .font(.cavnar(.caption).weight(.semibold))
                                .foregroundStyle(Color.cavnarInk3)
                                .rotationEffect(.degrees(expanded ? 180 : 0))
                                .accessibilityHidden(true)
                        }
                    }
                    .padding(.vertical, CavnarSpace.xs)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(isHeroDay ? StaffTodayPlan.accessibilityLabel(day) + ". Your next shift, shown above"
                                              : StaffTodayPlan.accessibilityLabel(day))
                .accessibilityValue(canExpand ? (expanded ? "Expanded" : "Collapsed") : "")
                .accessibilityAddTraits(canExpand ? .isButton : [])
                .accessibilityRemoveTraits(canExpand ? [] : .isButton)
                if !changeable.isEmpty && !isHeroDay {
                    Menu {
                        ForEach(Array(changeable.enumerated()), id: \.offset) { _, leg in
                            let suffix = changeable.count > 1 ? " \(leg.timeRange)" : ""
                            if leg.canDrop { Button("Give up shift\(suffix)") { onChange(leg, .drop) } }
                            if leg.canSwap { Button("Swap shift\(suffix)") { onChange(leg, .swap) } }
                        }
                    } label: {
                        Image(systemName: "ellipsis")
                            .font(.cavnar(.body).weight(.semibold))
                            .foregroundStyle(Color.cavnarInk2)
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .accessibilityLabel("Change shift")
                    .accessibilityHint("Give up shift or swap shift")
                }
            }
            if expanded {
                VStack(alignment: .leading, spacing: CavnarSpace.s) {
                    ForEach(Array(day.legs.enumerated()), id: \.offset) { _, leg in
                        StaffLegBlock(leg: leg, timeRole: day.legs.count > 1 ? .figureS : nil)
                    }
                }
                .padding(.bottom, CavnarSpace.s)
                .transition(reduceMotion ? .identity : .opacity)
            }
        }
    }

    /// "Today 10/8/26 · 4pm – 10pm · Server" — weekday in the label face,
    /// the date and times in the number face, everything at full contrast.
    private var line: Text {
        let name = day.isToday ? "Today" : String(day.weekday.prefix(3))
        let what = isHeroDay ? "Next shift, above \u{2191}" : StaffTodayPlan.dayLine(day)
        return Text(name).font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
            + Text(" ").font(.cavnar(.body))
            + HomeMixedText.make(CavnarDate.mdy(day.date), role: .secondary, color: .cavnarInk2)
            + Text(" \u{00B7} ").font(.cavnar(.body)).foregroundStyle(Color.cavnarInk3)
            + HomeMixedText.make(what, role: .body, color: day.legs.isEmpty || isHeroDay ? .cavnarInk2 : .cavnarInk)
    }
}

/// A shift past the seven days (`upcoming`, UX-10), in the week's one-line
/// form.
struct StaffLaterRow: View {
    let date: String
    let legs: [StaffShift]

    var body: some View {
        let times = legs.map(\.timeRange).filter { !$0.isEmpty }.joined(separator: " + ")
        let role = legs.compactMap(\.role).first(where: { !$0.isEmpty })
        let what = [times.isEmpty ? nil : times, role].compactMap { $0 }.joined(separator: " \u{00B7} ")
        (Text(String((legs.first?.day ?? "").prefix(3))).font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
            + Text(" ").font(.cavnar(.body))
            + HomeMixedText.make(CavnarDate.mdy(date), role: .secondary, color: .cavnarInk2)
            + (what.isEmpty ? Text("") : Text(" \u{00B7} ").font(.cavnar(.body)).foregroundStyle(Color.cavnarInk3)
               + HomeMixedText.make(what, role: .body, color: .cavnarInk)))
            .fixedSize(horizontal: false, vertical: true)
            .padding(.vertical, CavnarSpace.xs)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .combine)
    }
}

// MARK: - The quiet action surface

/// The hero's quiet actions: the secondary look (white 5% surface, 1pt ink
/// hairline at 16%, ink text — CavnarSecondaryButtonStyle's), at least
/// 44pt tall. Ember stays on the hero's one primary.
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
            .font(.cavnar(.label))
            .foregroundStyle(Color.cavnarInk)
            .padding(.horizontal, CavnarSpace.xs)
            .padding(.vertical, CavnarSpace.s)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .fill(Color.white.opacity(pressed ? 0.09 : 0.05)))
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .strokeBorder(Color.cavnarInk.opacity(0.16), lineWidth: 1))
            .contentShape(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
    }
}
