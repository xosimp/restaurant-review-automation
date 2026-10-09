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
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(alignment: .center, spacing: CavnarSpace.s) {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("Waiting on you")
                    CavnarMixedText(sentence, role: .label)
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

    var body: some View {
        let hours = StaffTodayPlan.weekHours(stats: store.stats.value, shifts: shifts)
        let tips = store.earnings.value?.lastShift
        let tipsTotal = tips?.tipsTotal
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if hours != nil || tipsTotal != nil {
                HStack(alignment: .top, spacing: CavnarSpace.xs) {
                    if let hours {
                        DSRStatTile(label: hours.label, value: StaffTime.figure(hours.hours),
                                    detail: StaffTodayPlan.workedLine(store.stats.value) ?? "scheduled")
                    }
                    if let tips, let total = tipsTotal {
                        DSRStatTile(label: "Tips \u{00B7} \(tipsDay(tips))", value: StaffMoney.label(total),
                                    detail: store.earnings.value?.asOfLabel.map { "as of \($0)" })
                    }
                }
            }
            if let line = store.stats.value?.overtimeLine {
                CavnarMixedText(line, role: .secondary,
                                color: store.stats.value?.overtime?.over == true ? .cavnarAmber : .cavnarInk2)
            }
            // Why there are no tips yet (the POS lags a day) — only when
            // the tile is missing; beside a figure it is noise.
            if tipsTotal == nil, let lag = store.earnings.value?.lagNote, !lag.isEmpty {
                Text(lag)
                    .cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if store.stats.phase == .failed && hours == nil {
                StaffLoadFailed(what: "your hours", message: store.stats.error) { await store.reloadStats() }
            }
        }
    }

    private func tipsDay(_ shift: StaffEarnings.Shift) -> String {
        let weekday = String((shift.weekday ?? "").prefix(3))
        return weekday.isEmpty ? (shift.dateLabel ?? CavnarDate.mdy(shift.businessDate)) : weekday
    }
}

// MARK: - Before service (H16 / V3)

/// The personal pre-shift brief, only on a working day (UX-36): the
/// manager-approved brief first, the focus item, then the day's lines —
/// the reader's own role and hours are the hero's, so they're left out.
/// Tags are neutral ink; a stock line ("86 risk", "86'd") is amber.
struct StaffBriefCard: View {
    let section: StaffSection<StaffPersonalBrief>
    let reload: () async -> Void

    private static let tags = ["volume": "Volume", "rush": "Rush", "watch": "Watch", "stock": "86 risk",
                               "eighty_sixed": "86\u{2019}d", "event": "Today", "weather": "Weather",
                               "promotion": "Promotion"]

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
                    CavnarMixedText("Tonight\u{2019}s focus: \(focus.item)", role: .label)
                    if let line = focus.line, !line.isEmpty {
                        CavnarMixedText(line, role: .secondary)
                    }
                }
            }
            ForEach(Array(brief.dayItems.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                    let tone: CavnarTone = (item.kind == "stock" || item.kind == "eighty_sixed") ? .warning : .neutral
                    Text(Self.tags[item.kind] ?? item.kind.capitalized)
                        .cavnarText(.tag, color: tone.foreground)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Capsule().fill(tone.background))
                    CavnarMixedText(item.text, role: .body, color: .cavnarInk)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
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

    private var choices: [Int] { store.late.value?.etaChoices ?? [10, 20, 30, 45] }
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
                    HStack(spacing: CavnarSpace.xs) {
                        ForEach(choices, id: \.self) { minutes in
                            chip(minutes)
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
            .accountSheetChrome("Running late")
        }
        .cavnarPostedOverlay(posted) { dismiss() }
        .presentationDetents([.medium, .large])
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
