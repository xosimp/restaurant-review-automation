import SwiftUI

// The employee's own settings (employee audit wave 2, I3): My availability
// by hours and dates (M5, B8) and What I'd like. Both open as sheets from
// Me (UX-31); a failed load never shows an editable form (UX-07/PERF-05).
// Same /staff/api/* routes the web portal used. (Today's "Before service"
// card is StaffBriefCard; Change PIN is I1's StaffChangePinView.)

// MARK: - Shared controls

/// A 44pt choice chip — ember outline and tint when on (DS `.lb2-days`).
struct StaffChoiceChip: View {
    let title: String
    let on: Bool
    var isNumber: Bool = false
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            Text(title)
                .font(isNumber ? .cavnarNumber(14.5, weight: 600) : .cavnarBody(14.5, weight: 600))
                .foregroundStyle(on ? Color.cavnarInk : Color.cavnarInk2)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(on ? Color.cavnarEmber.opacity(0.18) : Color.cavnarPaper3.opacity(0.45),
                            in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(on ? Color.cavnarEmber : Color.clear, lineWidth: 1.5))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

extension StaffDay {
    /// A picked day ↔ its ISO string on the phone's own calendar (what the
    /// DatePicker shows), so a date never drifts by the time zone.
    static func localDate(_ iso: String?) -> Date? {
        guard let iso else { return nil }
        let p = iso.prefix(10).split(separator: "-")
        guard p.count == 3, let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]) else { return nil }
        return Calendar.current.date(from: DateComponents(year: y, month: m, day: d))
    }

    static var todayISO: String { CavnarDate.isoDay(Date()) }
}

// MARK: - My availability (GET/POST /staff/api/availability)

/// `StaffAvailabilitySheet()` — each weekday any time, can't work, or some
/// hours, either with optional dates; always saved with the `updated_at`
/// it loaded. A 409 replaces the form with the record as it is now.
struct StaffAvailabilitySheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss
    @State private var load: StaffLoad<StaffAvailabilityRecord> = .loading
    @State private var draft: StaffAvailabilityDraft?
    @State private var openDay: String?
    @State private var openTime: String?
    @State private var problems: [String: String] = [:]
    @State private var saving = false
    @State private var saveError: String?
    @State private var staleNote: String?
    @State private var conflicts: [StaffAvailabilityConflict] = []
    @State private var conflictsText = ""
    @State private var hint: StaffHint?
    @State private var inlinePosted = false
    @State private var posted: String?
    @State private var giving: StaffAvailabilityConflict?
    @State private var askingTimeOff = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch load {
                    case .loading:
                        StaffUI.loading("Loading your availability")
                    case .failed(let message):
                        // No form after a failed load: a save from here
                        // would overwrite what's stored (PERF-05).
                        StaffUI.failedCard(message) { Task { await reload() } }
                    case .loaded(let record):
                        form(record)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("My availability")
        }
        .task { await reload() }
        .cavnarPostedOverlay(posted) { dismiss() }
        .sheet(item: $giving) { c in
            StaffShiftChangeSheet(date: c.date, shiftStart: c.shiftStart, shiftEnd: c.shiftEnd, mode: .drop)
                .environment(staff)
        }
        .sheet(isPresented: $askingTimeOff) {
            StaffTimeOffSheet().environment(staff)
        }
    }

    @ViewBuilder
    private func form(_ record: StaffAvailabilityRecord) -> some View {
        if let staleNote {
            StaffUI.note(staleNote, color: .cavnarAmber)
                .padding(12)
                .background(Color.cavnarAmberBg, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
        }
        StaffUI.note(record.timeOffHint ?? "Your usual week. Dates you\u{2019}re away go in a time-off request.")
        if draft != nil {
            AccountSection(kicker: "Your usual week") {
                ForEach(Array(StaffAvailabilityRecord.weekdays.enumerated()), id: \.element) { i, day in
                    dayEditor(i)
                    if i < 6 { AccountRowDivider() }
                }
            }
            VStack(alignment: .leading, spacing: 6) {
                AccountKicker(text: "Anything else")
                TextField("\u{201C}Rides the bus — not before 10am\u{201D}", text: notesBinding, axis: .vertical)
                    .lineLimit(1...4)
                    .cavnarTextFieldStyle()
                    .onChange(of: draft?.notes ?? "") { _, v in
                        if v.count > 300 { draft?.notes = String(v.prefix(300)) }
                    }
            }
            if let whole = problems[""] { StaffUI.errorLine(whole) }
            Button {
                Task { await save() }
            } label: {
                Group {
                    if saving { StaffShimmerLabel(text: "Saving") } else { Text("Save my availability") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: saving))
            .disabled(saving)
            if let saveError { StaffUI.errorLine(saveError) }
            if inlinePosted {
                CavnarInlinePosted(label: "Saved") { inlinePosted = false }
            }
            if !conflicts.isEmpty { conflictsCard }
            if let hint {
                VStack(alignment: .leading, spacing: 2) {
                    StaffUI.note(hint.text)
                    StaffTextButton(title: "Ask for time off") { askingTimeOff = true }
                }
                .cavnarCard()
            }
        }
    }

    private var conflictsCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            StaffUI.note(conflictsText.isEmpty ? "You\u{2019}re still on shifts this rules out." : conflictsText,
                         color: .cavnarAmber)
            ForEach(conflicts) { c in
                VStack(alignment: .leading, spacing: 2) {
                    HomeMixedText.make(c.label + (c.role.map { " · \($0)" } ?? ""), size: CavnarType.body,
                                       weight: 700, color: .cavnarInk)
                    if let reason = c.reason {
                        Text(reason.prefix(1).uppercased() + reason.dropFirst())
                            .font(.cavnarBody(CavnarType.secondary)).foregroundStyle(Color.cavnarInk3)
                    }
                    StaffTextButton(title: "Give up this shift") { giving = c }
                }
            }
            StaffUI.note("Your manager has been told about these.")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    // MARK: One weekday

    private func dayEditor(_ i: Int) -> some View {
        let day = draft?.days[i] ?? StaffAvailabilityDay(day: StaffAvailabilityRecord.weekdays[i])
        return VStack(alignment: .leading, spacing: 6) {
            CavnarDropdown(title: day.day, subtitle: day.summary,
                           isExpanded: Binding(get: { openDay == day.day },
                                               set: { openDay = $0 ? day.day : nil })) {
                VStack(alignment: .leading, spacing: 12) {
                    // Chips, not the segmented control: its zero-distance
                    // drag would take the scroll away from the sheet.
                    HStack(spacing: 6) {
                        ForEach(StaffAvailabilityDay.Status.allCases, id: \.self) { status in
                            StaffChoiceChip(title: status.label, on: day.status == status) {
                                statusBinding(i).wrappedValue = status
                            }
                        }
                    }
                    if day.status == .window {
                        timeChoice(i, label: "No start before", key: "\(day.day)|e", value: day.earliest) {
                            draft?.days[i].earliest = $0
                        }
                        timeChoice(i, label: "Done by", key: "\(day.day)|l", value: day.latest) {
                            draft?.days[i].latest = $0
                        }
                    }
                    if day.status != .any {
                        dateRow(label: "Starting", value: day.from, allowPast: true) { draft?.days[i].from = $0 }
                        dateRow(label: "Until", value: day.until, allowPast: false) { draft?.days[i].until = $0 }
                        StaffUI.note(day.from == nil && day.until == nil
                                     ? "Every week, until you change it."
                                     : "Only between these dates.")
                    }
                }
                .padding(.bottom, 6)
            }
            if let p = problems[day.day] { StaffUI.errorLine(p) }
        }
        .padding(.vertical, 8)
        .accessibilityElement(children: .contain)
    }

    private func statusBinding(_ i: Int) -> Binding<StaffAvailabilityDay.Status> {
        Binding(get: { draft?.days[i].status ?? .any },
                set: { new in
                    draft?.days[i].status = new
                    if let d = draft?.days[i].day { problems[d] = nil }
                    problems[""] = nil
                })
    }

    private var notesBinding: Binding<String> {
        Binding(get: { draft?.notes ?? "" }, set: { draft?.notes = $0 })
    }

    /// A time of day: the half-hour choices in a grid, folded until opened
    /// (never a system wheel — DS §7), with "No limit".
    private func timeChoice(_ i: Int, label: String, key: String, value: String?,
                            set: @escaping (String?) -> Void) -> some View {
        CavnarDropdown(title: label, subtitle: value.map { StaffClock.display($0) } ?? "No limit",
                       isExpanded: Binding(get: { openTime == key }, set: { openTime = $0 ? key : nil })) {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 6), count: 4), spacing: 6) {
                StaffChoiceChip(title: "No limit", on: value == nil) {
                    set(nil)
                    openTime = nil
                }
                ForEach(StaffClock.choices, id: \.self) { t in
                    StaffChoiceChip(title: StaffClock.display(t), on: StaffClock.minutes(value) == StaffClock.minutes(t),
                                    isNumber: true) {
                        set(t)
                        openTime = nil
                        problems[StaffAvailabilityRecord.weekdays[i]] = nil
                    }
                }
            }
        }
    }

    private func dateRow(label: String, value: String?, allowPast: Bool,
                         set: @escaping (String?) -> Void) -> some View {
        HStack(spacing: 10) {
            if let value, let date = StaffDay.localDate(value) {
                DatePicker(label, selection: Binding(get: { date }, set: { set(StaffDay.iso($0)) }),
                           in: (allowPast ? Date.distantPast : Calendar.current.startOfDay(for: Date()))...,
                           displayedComponents: .date)
                    .font(.cavnarBody(CavnarType.body))
                    .foregroundStyle(Color.cavnarInk3)
                    .tint(Color.cavnarEmber)
                StaffTextButton(title: "Clear", tone: .cavnarInk3) { set(nil) }
                    .accessibilityLabel("Clear the \(label.lowercased()) date")
            } else {
                Text(label).font(.cavnarBody(CavnarType.body)).foregroundStyle(Color.cavnarInk3)
                Spacer(minLength: 8)
                StaffTextButton(title: label == "Until" ? "Add an end date" : "Add a start date") {
                    set(StaffDay.todayISO)
                }
            }
        }
        .frame(minHeight: 44)
    }

    // MARK: Load and save

    private func reload() async {
        load = .loading
        do {
            let r: StaffAvailabilityRecord = try await staff.authed("/staff/api/availability")
            adopt(r)
        } catch {
            draft = nil
            load = .failed(StaffErrorText.message(error, fallback: "Your availability didn\u{2019}t load."))
        }
    }

    private func adopt(_ r: StaffAvailabilityRecord) {
        load = .loaded(r)
        draft = StaffAvailabilityDraft(r)
    }

    private func save() async {
        guard let d = draft else { return }
        problems = d.problems(today: StaffDay.todayISO)
        if let first = StaffAvailabilityRecord.weekdays.first(where: { problems[$0] != nil }) {
            openDay = first
            return
        }
        guard problems.isEmpty else { return }
        saving = true
        saveError = nil
        staleNote = nil
        hint = nil
        conflicts = []
        defer { saving = false }
        do {
            let r: StaffAvailabilitySaved = try await staff.authed("/staff/api/availability", method: .post, body: d.body())
            Haptic.success()
            adopt(r.record)
            conflicts = r.conflicts
            conflictsText = r.conflictsText
            hint = r.hint
            openDay = nil
            if r.conflicts.isEmpty && r.hint == nil {
                posted = "Saved — your manager\u{2019}s next schedule reads it"
            } else {
                inlinePosted = true
            }
        } catch let e as APIClient.APIError {
            let refusal = e.decodeBody(StaffAvailabilityRefusal.self)
            if e.status == 409, let latest = refusal?.availability {
                // Never retried with the new version: the person redoes it.
                adopt(latest)
                openDay = nil
                staleNote = "Someone changed this \u{2014} here\u{2019}s the latest. Make your change again."
            } else {
                saveError = refusal?.error ?? StaffErrorText.message(e)
                hint = refusal?.hint
            }
        } catch {
            saveError = StaffErrorText.message(error)
        }
    }
}

// MARK: - What I'd like (GET/POST /staff/api/preferences)

/// `StaffPreferencesSheet()` — dayparts and hours a week: a wish the draft
/// honours where coverage allows. The texts switch lives on Me.
struct StaffPreferencesSheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss
    @State private var load: StaffLoad<StaffPreferencesState> = .loading
    @State private var dayparts: Set<String> = []
    @State private var hours = ""
    @State private var saving = false
    @State private var error: String?
    @State private var posted: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch load {
                    case .loading:
                        StaffUI.loading("Loading what you\u{2019}d like")
                    case .failed(let message):
                        StaffUI.failedCard(message) { Task { await reload() } }
                    case .loaded:
                        form
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("What I\u{2019}d like")
        }
        .task { await reload() }
        .cavnarPostedOverlay(posted) { dismiss() }
    }

    @ViewBuilder
    private var form: some View {
        StaffUI.note("The shifts you\u{2019}d rather have, and how many hours a week you want.")
        AccountSection(kicker: "Shifts you\u{2019}d rather have") {
            HStack(spacing: 8) {
                StaffChoiceChip(title: "Days", on: dayparts.contains("morning")) { flip("morning") }
                StaffChoiceChip(title: "Nights", on: dayparts.contains("night")) { flip("night") }
            }
            .padding(.vertical, 9)
        }
        AccountSection(kicker: "Hours") {
            AccountKVRow(label: "Hours a week", showsDivider: false) {
                TextField("\u{2014}", text: $hours)
                    .keyboardType(.numberPad)
                    .multilineTextAlignment(.trailing)
                    .font(.cavnarNumber(17, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .frame(width: 70)
                    .accessibilityLabel("Hours a week")
                    .onChange(of: hours) { _, v in
                        let digits = String(v.filter(\.isNumber).prefix(2))
                        if digits != v { hours = digits }
                    }
            }
        }
        StaffUI.note("A wish, not a rule \u{2014} the schedule honours it where coverage allows.")
        Button {
            Task { await save() }
        } label: {
            Group {
                if saving { StaffShimmerLabel(text: "Saving") } else { Text("Save") }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: saving))
        .disabled(saving)
        if let error { StaffUI.errorLine(error) }
    }

    private func flip(_ part: String) {
        if dayparts.contains(part) { dayparts.remove(part) } else { dayparts.insert(part) }
    }

    private func reload() async {
        load = .loading
        do {
            let r: StaffPreferencesState = try await staff.authed("/staff/api/preferences")
            dayparts = Set(r.preferredDayparts)
            hours = r.desiredHours.map { String(Int($0.rounded())) } ?? ""
            load = .loaded(r)
        } catch {
            load = .failed(StaffErrorText.message(error, fallback: "What you\u{2019}d like didn\u{2019}t load."))
        }
    }

    private func save() async {
        saving = true
        error = nil
        defer { saving = false }
        let order = ["morning", "night"].filter { dayparts.contains($0) }
        do {
            let r: StaffOKResponse = try await staff.authed(
                "/staff/api/preferences", method: .post,
                body: StaffPreferencesSaveBody(preferredDayparts: order, desiredHours: Int(hours)))
            if r.ok {
                Haptic.success()
                posted = "Saved — the next draft reads it"
            } else {
                error = r.error ?? "Could not save."
            }
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}
