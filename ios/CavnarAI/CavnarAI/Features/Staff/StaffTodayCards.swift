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
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    StaffKicker(text: "Waiting on you")
                    HomeMixedText.make(sentence, size: CavnarType.body, weight: 600, color: .cavnarInk)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 14, weight: .semibold))
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
        VStack(alignment: .leading, spacing: 8) {
            if hours != nil || tips != nil {
                HStack(alignment: .top, spacing: 8) {
                    if let hours {
                        DSRStatTile(label: hours.label, value: StaffTime.figure(hours.hours),
                                    detail: StaffTodayPlan.workedLine(store.stats.value) ?? "scheduled")
                    }
                    if let tips, let total = tips.tipsTotal {
                        DSRStatTile(label: "Tips \u{00B7} \(tipsDay(tips))", value: StaffMoney.label(total),
                                    detail: store.earnings.value?.asOfLabel.map { "POS as of \($0)" })
                    }
                }
            }
            if let line = store.stats.value?.overtimeLine {
                HomeMixedText.make(line, size: CavnarType.secondary, weight: 600,
                                   color: store.stats.value?.overtime?.over == true ? .cavnarAmber : .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if tips != nil, let lag = store.earnings.value?.lagNote, !lag.isEmpty {
                Text(lag)
                    .font(.cavnarBody(CavnarType.caption))
                    .foregroundStyle(Color.cavnarInk3)
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
            VStack(alignment: .leading, spacing: 8) {
                StaffKicker(text: "Before service")
                StaffLoadFailed(what: "today\u{2019}s brief", message: section.error, retry: reload)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        case .loading:
            EmptyView()
        }
    }

    private func card(_ brief: StaffPersonalBrief) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            StaffKicker(text: "Before service")
            if let text = brief.briefText, !text.isEmpty {
                HomeMixedText.make(text, size: CavnarType.body, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let focus = brief.focus {
                VStack(alignment: .leading, spacing: 2) {
                    HomeMixedText.make("Tonight\u{2019}s focus: \(focus.item)", size: CavnarType.body, weight: 700,
                                       color: .cavnarInk)
                    if let line = focus.line, !line.isEmpty {
                        HomeMixedText.make(line, size: CavnarType.secondary, color: .cavnarInk2)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(brief.dayItems.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    let tone: CavnarTone = (item.kind == "stock" || item.kind == "eighty_sixed") ? .warning : .neutral
                    Text((Self.tags[item.kind] ?? item.kind.capitalized).uppercased())
                        .font(.cavnarBody(10.5, weight: 700))
                        .kerning(0.6)
                        .foregroundStyle(tone.foreground)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(tone.background))
                    HomeMixedText.make(item.text, size: CavnarType.secondary + 1, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}

// MARK: - Who's on with me (H7)

struct StaffCoworkersCard: View {
    let section: StaffSection<StaffCoworkersResponse>
    let hero: StaffHero
    let reload: () async -> Void

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
            VStack(alignment: .leading, spacing: 8) {
                StaffKicker(text: title)
                StaffLoadFailed(what: "who\u{2019}s on", message: section.error, retry: reload)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        case .ready:
            if let r = section.value, r.date == hero.day.date || r.date == nil {
                let people = r.coworkers ?? []
                VStack(alignment: .leading, spacing: 10) {
                    StaffKicker(text: title)
                    if people.isEmpty {
                        Text(r.posted == false ? "That day isn\u{2019}t posted yet."
                                               : "Nobody else is on the schedule that day.")
                            .font(.cavnarBody(CavnarType.secondary))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    ForEach(people) { person in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(person.name)
                                    .font(.cavnarBody(CavnarType.body, weight: 700))
                                    .foregroundStyle(Color.cavnarInk)
                                let what = [person.role, person.station.map { "On \($0)" }]
                                    .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " \u{00B7} ")
                                if !what.isEmpty {
                                    Text(what)
                                        .font(.cavnarBody(CavnarType.secondary))
                                        .foregroundStyle(Color.cavnarInk2)
                                }
                            }
                            Spacer(minLength: 8)
                            if !person.timeRange.isEmpty {
                                Text(person.timeRange)
                                    .font(.cavnarNumber(CavnarType.secondary, weight: 600))
                                    .foregroundStyle(Color.cavnarInk2)
                                    .lineLimit(1)
                                    .minimumScaleFactor(0.8)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard()
            }
        }
    }
}

// MARK: - A guest named you (V5)

struct StaffRecognitionCard: View {
    let section: StaffSection<StaffRecognition>

    var body: some View {
        if let r = section.value, let first = r.items?.first {
            VStack(alignment: .leading, spacing: 8) {
                StaffKicker(text: "A guest named you")
                if let excerpt = first.excerpt, !excerpt.isEmpty {
                    HomeMixedText.make("\u{201C}\(excerpt)\u{201D}", size: CavnarType.body, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("A guest named you in a review.")
                        .font(.cavnarBody(CavnarType.body))
                        .foregroundStyle(Color.cavnarInk)
                }
                let count = r.count ?? r.items?.count ?? 1
                let when = first.dateLabel ?? first.date.map(CavnarDate.mdy) ?? ""
                HomeMixedText.make(count > 1 ? "\(when) \u{00B7} \(count) mentions this year" : when,
                                   size: CavnarType.caption, color: .cavnarInk3)
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
                VStack(alignment: .leading, spacing: 16) {
                    HomeMixedText.make("For your \(StaffTime.label(leg.shiftStart ?? "")) shift today",
                                       size: CavnarType.emphasis, weight: 700, color: .cavnarInk)
                    if let existing {
                        HomeMixedText.make("You already said about \(existing.etaMinutes) min. Sending again updates it; your manager isn\u{2019}t paged twice.",
                                           size: CavnarType.secondary, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    StaffKicker(text: "About how late?")
                    HStack(spacing: 8) {
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
                            .font(.cavnarBody(CavnarType.secondary, weight: 600))
                            .foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text("Running much later than 45 minutes? Call your manager, or ask to give the shift up.")
                        .font(.cavnarBody(CavnarType.caption))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(20)
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
                .font(.cavnarNumber(CavnarType.body, weight: 600))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk)
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
