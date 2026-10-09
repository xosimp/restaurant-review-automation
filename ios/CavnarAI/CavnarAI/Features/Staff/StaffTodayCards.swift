import SwiftUI

// Today's supporting cards (employee audit V1, V2, V5, H7, H16, H1). Each
// draws its own section's state: the pulse while loading, the failure with
// Try again, and nothing at all when there is honestly nothing to say —
// never an empty card, never a zero standing in for "unknown".

// MARK: - Waiting on you

/// Only when a swap or an offer waits on this person; one tap opens
/// Requests, where they answer it (UX-06).
struct StaffWaitingStrip: View {
    let sentence: String
    /// The name the sentence opens with, set in the label weight; the rest
    /// reads in body (re-audit L9 — a whole bold sentence shouted).
    var lead: String? = nil
    let open: () -> Void

    private var text: Text {
        if let lead, !lead.isEmpty, sentence.hasPrefix(lead) {
            return HomeMixedText.make(lead, role: .label, color: .cavnarInk)
                + HomeMixedText.make(String(sentence.dropFirst(lead.count)), role: .body, color: .cavnarInk)
        }
        return HomeMixedText.make(sentence, role: .body, color: .cavnarInk)
    }

    var body: some View {
        Button(action: open) {
            HStack(alignment: .center, spacing: CavnarSpace.s) {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("Waiting on you")
                    text
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.secondary).weight(.semibold))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .cavnarCard()
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Waiting on you. \(sentence)")
        .accessibilityHint("Opens Requests")
    }
}

// MARK: - From your manager (urgent and unread)

/// Under the hero, only for an urgent announcement not yet "Got it" or a
/// manager's reply not yet read (`StaffManagerStrip`). An urgent one is
/// ringed red, as in the inbox; a tap opens the inbox (or, for replies
/// alone, the thread). Everything else stays behind the tray.
struct StaffManagerStripView: View {
    let text: String
    let urgent: Bool
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(alignment: .center, spacing: CavnarSpace.s) {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("From your manager", icon: urgent ? "exclamationmark.circle" : "bubble.left",
                                 tint: urgent ? .cavnarRedText : .cavnarEmber2)
                    CavnarMixedText(text, role: .label)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.secondary).weight(.semibold))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .cavnarCard()
            .overlay {
                if urgent {
                    RoundedRectangle(cornerRadius: CavnarRadius.card)
                        .strokeBorder(Color.cavnarRed.opacity(0.7), lineWidth: 1.5)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("From your manager. \(text)")
        .accessibilityHint(urgent ? "Opens your inbox" : "Opens your messages")
    }
}

// MARK: - Hours and tips (V2, H11)

/// Hours this week (scheduled, and worked through the POS's last day), the
/// overtime heads-up in hours only, and the last shift's tips — the
/// person's own figures, each with its "as of" (B6).
struct StaffStatsTiles: View {
    let store: StaffPortalStore
    let shifts: StaffShiftsResponse
    /// The tips tile's "where these come from" note, behind a tap on the
    /// tile (re-audit M2).
    @State private var showingTipsNote = false

    var body: some View {
        let hours = StaffTodayPlan.weekHours(stats: store.stats.value, shifts: shifts)
        let earnings = store.earnings.value
        let tips = earnings?.lastShift
        let tipsTotal = tips?.tipsTotal
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if hours != nil || tipsTotal != nil {
                HStack(alignment: .top, spacing: CavnarSpace.xs) {
                    if let hours {
                        DSRStatTile(label: hours.label, value: StaffTime.figure(hours.hours),
                                    detail: StaffTodayPlan.workedLine(store.stats.value) ?? "scheduled")
                    }
                    if let tips, let total = tipsTotal {
                        Button {
                            Haptic.selection()
                            showingTipsNote.toggle()
                        } label: {
                            DSRStatTile(label: "Tips \u{00B7} \(tipsDay(tips))", value: StaffMoney.label(total),
                                        detail: earnings?.asOfLabel.map { "as of \($0)" })
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint(showingTipsNote ? "Hides where tips come from" : "Shows where tips come from")
                    }
                }
            }
            // The week against the overtime line, then the heads-up in
            // words (re-audit V1).
            if let meter = store.stats.value?.overtimeMeter {
                StaffHoursMeter(hours: meter.hours, line: meter.line, projected: meter.projected)
            }
            if let line = store.stats.value?.overtimeLine {
                CavnarMixedText(line, role: .secondary,
                                color: store.stats.value?.overtime?.over == true ? .cavnarAmber : .cavnarInk2)
            }
            if showingTipsNote, tipsTotal != nil, let note = earnings?.tipsNote, !note.isEmpty {
                Text(note)
                    .cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Why there is no tips tile, only when there's something true
            // to say (re-audit M1): the POS lag while nothing is in yet,
            // or the server's reason it can't find this person's punches.
            if tipsTotal == nil, let line = earnings?.missingTipsLine {
                Text(line)
                    .cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if store.stats.phase == .failed && hours == nil {
                StaffLoadFailed(what: "your hours", message: store.stats.error) { await store.reloadStats() }
            }
        }
    }

    /// The day the tips are for, M/D/YY (re-audit M2: a bare "Wed" didn't
    /// say which Wednesday).
    private func tipsDay(_ shift: StaffEarnings.Shift) -> String {
        if let label = shift.dateLabel, !label.isEmpty { return label }
        return CavnarDate.mdy(shift.businessDate)
    }
}

/// The week's hours against the overtime line (re-audit V1): a thin bar
/// with the line marked, Ink2 under it and amber past it. The figure is
/// the server's projection (worked so far plus what's still scheduled),
/// else the scheduled hours.
struct StaffHoursMeter: View {
    let hours: Double
    let line: Double
    /// The figure is the server's projection, not just the schedule.
    var projected: Bool = false

    private var over: Bool { hours > line }
    private var scale: Double { max(line * 1.25, hours) }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            GeometryReader { geo in
                let w = geo.size.width
                let fill = max(4, w * CGFloat(min(hours, scale) / scale))
                let mark = w * CGFloat(line / scale)
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.7))
                        .frame(height: 6)
                    Capsule().fill(over ? Color.cavnarAmber : Color.cavnarInk2)
                        .frame(width: fill, height: 6)
                    Rectangle().fill(Color.cavnarInk)
                        .frame(width: 2, height: 12)
                        .offset(x: mark - 1)
                }
                .frame(height: 12)
            }
            .frame(height: 12)
            HStack(spacing: CavnarSpace.xs) {
                HomeMixedText.make(projected ? "On course for \(StaffTime.figure(hours))h"
                                             : "\(StaffTime.figure(hours))h scheduled",
                                   role: .caption, color: over ? .cavnarAmber : .cavnarInk2)
                Spacer(minLength: CavnarSpace.xs)
                HomeMixedText.make("Overtime past \(StaffTime.figure(line))h", role: .caption, color: .cavnarInk2)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(projected ? "On course for" : "Scheduled for") \(StaffTime.figure(hours)) hours this week. Overtime starts past \(StaffTime.figure(line)) hours.")
    }
}

// MARK: - Before service (H16 / V3)

/// The personal pre-shift brief, only on a working day (UX-36): the
/// manager-approved brief first, the focus item, then the day's lines —
/// the reader's own role and hours are the hero's, so they're left out.
/// Tags are neutral ink; a stock line ("86 risk", "86'd") is amber.
struct StaffBriefCard: View {
    let section: StaffSection<StaffPersonalBrief>
    /// "Tonight's focus" before a dinner shift, "Today's focus" otherwise
    /// (re-audit L7 — a lunch shift read "Tonight's").
    var focusLabel: String = "Today\u{2019}s focus"
    let reload: () async -> Void

    /// The day's lines shown before "+N more" (re-audit L8).
    static let shownItems = 3

    private static let tags = ["volume": "Volume", "rush": "Rush", "watch": "Watch", "stock": "86 risk",
                               "eighty_sixed": "86\u{2019}d", "event": "Today", "weather": "Weather",
                               "promotion": "Promotion"]

    /// The focus line's label for the hero's shift: "Tonight's focus" when
    /// today's next leg is an evening one, else "Today's focus".
    static func focusLabel(_ hero: StaffHero?) -> String {
        guard let hero, hero.day.isToday, hero.title == "Tonight" else { return "Today\u{2019}s focus" }
        return "Tonight\u{2019}s focus"
    }

    /// An event line under the "Today" tag without its own "Today: "
    /// (re-audit L7: "TODAY  Today: Bears game.").
    static func itemText(_ item: StaffBriefItem) -> String {
        guard item.kind == "event", item.text.hasPrefix("Today: ") else { return item.text }
        return String(item.text.dropFirst("Today: ".count))
    }

    var body: some View {
        switch section.phase {
        case .ready:
            if let brief = section.value, brief.shouldShow {
                card(brief)
            }
        case .failed:
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Before service")
                StaffLoadFailed(what: "today\u{2019}s brief", message: section.error, retry: reload)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        case .loading:
            EmptyView()
        }
    }

    private func card(_ brief: StaffPersonalBrief) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Before service")
            if let text = brief.briefText, !text.isEmpty {
                CavnarMixedText(text, role: .body, color: .cavnarInk)
            }
            if let focus = brief.focus {
                VStack(alignment: .leading, spacing: 2) {
                    CavnarMixedText("\(focusLabel): \(focus.item)", role: .label)
                    if let line = focus.line, !line.isEmpty {
                        CavnarMixedText(line, role: .secondary)
                    }
                }
            }
            let items = brief.dayItems
            ForEach(Array(items.prefix(Self.shownItems).enumerated()), id: \.offset) { _, item in
                itemRow(item)
            }
            if items.count > Self.shownItems {
                CavnarMoreDisclosure(hiddenCount: items.count - Self.shownItems) {
                    ForEach(Array(items.dropFirst(Self.shownItems).enumerated()), id: \.offset) { _, item in
                        itemRow(item)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func itemRow(_ item: StaffBriefItem) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            let tone: CavnarTone = (item.kind == "stock" || item.kind == "eighty_sixed") ? .warning : .neutral
            Text(Self.tags[item.kind] ?? item.kind.capitalized)
                .cavnarText(.tag, color: tone.foreground)
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(Capsule().fill(tone.background))
            CavnarMixedText(Self.itemText(item), role: .body, color: .cavnarInk)
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Who's on with me (H7)

/// The first four on the hero's day — the ones whose hours overlap yours
/// first — and "Show all N" for the rest.
struct StaffCoworkersCard: View {
    let section: StaffSection<StaffCoworkersResponse>
    let hero: StaffHero
    let reload: () async -> Void

    static let shown = 4

    private var title: String {
        switch hero.title {
        case "Today", "Tonight": return "On with you today"
        case "Tomorrow": return "On with you tomorrow"
        default: return "On with you \(hero.day.weekday)"
        }
    }

    var body: some View {
        switch section.phase {
        case .loading:
            EmptyView()
        case .failed:
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker(title)
                StaffLoadFailed(what: "who\u{2019}s on", message: section.error, retry: reload)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        case .ready:
            if let r = section.value, r.date == hero.day.date || r.date == nil {
                let people = StaffTodayPlan.coworkersByOverlap(r.coworkers ?? [], legs: hero.day.legs)
                VStack(alignment: .leading, spacing: CavnarSpace.s) {
                    CavnarKicker(title)
                    if people.isEmpty {
                        Text(r.posted == false ? "That day isn\u{2019}t posted yet."
                                               : "Nobody else is on the schedule that day.")
                            .cavnarText(.secondary)
                    }
                    ForEach(people.prefix(Self.shown)) { row($0) }
                    if people.count > Self.shown {
                        CavnarMoreDisclosure(hiddenCount: people.count - Self.shown, total: people.count) {
                            ForEach(people.dropFirst(Self.shown)) { row($0) }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard()
            }
        }
    }

    private func row(_ person: StaffCoworker) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            VStack(alignment: .leading, spacing: 2) {
                Text(person.name).cavnarText(.label)
                let what = [person.role, person.station.map { "On \($0)" }]
                    .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " \u{00B7} ")
                if !what.isEmpty {
                    Text(what).cavnarText(.secondary)
                }
            }
            Spacer(minLength: CavnarSpace.xs)
            if !person.timeRange.isEmpty {
                Text(person.timeRange)
                    .font(.cavnarNumber(CavnarType.secondary, weight: 600))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - A guest named you (V5)

struct StaffRecognitionCard: View {
    let section: StaffSection<StaffRecognition>

    var body: some View {
        if let r = section.value, let first = r.items?.first {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("A guest named you")
                if let excerpt = first.excerpt, !excerpt.isEmpty {
                    CavnarMixedText("\u{201C}\(excerpt)\u{201D}", role: .body, color: .cavnarInk)
                } else {
                    Text("A guest named you in a review.")
                        .cavnarText(.body, color: .cavnarInk)
                }
                let count = r.count ?? r.items?.count ?? 1
                let when = first.dateLabel ?? first.date.map(CavnarDate.mdy) ?? ""
                CavnarMixedText(count > 1 ? "\(when) \u{00B7} \(count) mentions this year" : when, role: .caption)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
            .accessibilityElement(children: .combine)
        }
    }
}

// MARK: - Running late (H1)

/// ETA chips (10 · 20 · 30 · 45, the server's own), an optional note, and
/// one primary: "Tell my manager". Only for today's shift; the server
/// tells the managers once and a second report updates the ETA.
struct StaffRunningLateSheet: View {
    let store: StaffPortalStore
    let date: String
    let leg: StaffShift

    @Environment(\.dismiss) private var dismiss
    @State private var eta: Int?
    @State private var note = ""
    @State private var sending = false
    @State private var error: String?
    @State private var posted: String?

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var choices: [Int] { store.late.value?.etaChoices ?? [10, 20, 30, 45] }
    private var isDirty: Bool {
        posted == nil && !sending && !note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    private var existing: StaffRunningLateReport? { store.lateReport(date: date, shiftStart: leg.shiftStart) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.m) {
                    CavnarMixedText("For your \(StaffTime.label(leg.shiftStart ?? "")) shift today", role: .lead)
                    if let existing {
                        CavnarMixedText("You already said about \(existing.etaMinutes) min. Sending again updates it; your manager isn\u{2019}t paged twice.",
                                        role: .secondary)
                    }
                    CavnarKicker("About how late?")
                    // One row while the four fit; two by two at large text
                    // sizes (re-audit L6).
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: CavnarSpace.xs) {
                            ForEach(choices, id: \.self) { minutes in
                                chip(minutes).fixedSize(horizontal: true, vertical: false)
                                    .frame(maxWidth: .infinity)
                            }
                        }
                        LazyVGrid(columns: [GridItem(.flexible(), spacing: CavnarSpace.xs),
                                            GridItem(.flexible(), spacing: CavnarSpace.xs)],
                                  spacing: CavnarSpace.xs) {
                            ForEach(choices, id: \.self) { minutes in
                                chip(minutes)
                            }
                        }
                    }
                    TextField("Note for your manager (optional)", text: $note, axis: .vertical)
                        .lineLimit(1...3)
                        .cavnarTextFieldStyle()
                        .onChange(of: note) { _, v in if v.count > 200 { note = String(v.prefix(200)) } }
                    Button {
                        Task { await send() }
                    } label: {
                        VStack(spacing: 6) {
                            Text(existing == nil ? "Tell my manager" : "Update my ETA")
                            if sending { CavnarSkeletonBar(height: 3).frame(width: 90) }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: eta == nil || sending))
                    .disabled(eta == nil || sending)
                    if let error {
                        Text(error)
                            .cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text("Running much later than 45 minutes? Call your manager, or ask to give the shift up.")
                        .cavnarText(.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(CavnarSpace.gutter)
            }
            .background(Color.cavnarPaper.ignoresSafeArea())
            // A note typed and not sent isn't lost to a swipe (re-audit M3).
            .accountSheetChrome("Running late", isDirty: isDirty)
        }
        .cavnarPostedOverlay(posted) { dismiss() }
        // At accessibility text sizes "Tell my manager" would sit below a
        // medium sheet's fold (re-audit L6): open it full height.
        .presentationDetents(dynamicTypeSize.isAccessibilitySize ? [.large] : [.medium, .large])
        .onAppear { if eta == nil { eta = existing?.etaMinutes } }
    }

    private func chip(_ minutes: Int) -> some View {
        let on = eta == minutes
        return Button {
            Haptic.selection()
            eta = minutes
        } label: {
            Text("\(minutes) min")
                .cavnarText(.figureS, color: on ? Color.cavnarPaper : Color.cavnarInk)
                .lineLimit(1)
                .padding(.horizontal, CavnarSpace.xs)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                    .fill(on ? Color.cavnarInk : Color.cavnarPaper3.opacity(0.6)))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(minutes) minutes")
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private func send() async {
        guard let eta, let start = leg.shiftStart else { return }
        sending = true
        error = nil
        defer { sending = false }
        switch await store.reportLate(date: date, shiftStart: start, eta: eta, note: note) {
        case .told(let managersTold, let created):
            Haptic.success()
            if !created {
                posted = "ETA updated"
            } else {
                posted = managersTold ? "Your manager knows" : "Saved \u{2014} call your manager too"
            }
        case .refused(let message):
            Haptic.error()
            error = message
        }
    }
}
