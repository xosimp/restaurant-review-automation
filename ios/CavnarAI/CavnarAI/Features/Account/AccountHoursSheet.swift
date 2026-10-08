import SwiftUI

/// Account -> Profile -> "Hours & closures". Open/close per day and the
/// dates you're closed. Close times were already driving schedule
/// generation (the hard cap on a generated shift's end); open times and
/// closures are new and feed the same place.
struct AccountHoursSheet: View {
    let viewModel: AccountViewModel
    let profile: AccountProfile
    @Environment(\.dismiss) private var dismiss

    private static let days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    @State private var hours: [String: HoursDayDraft]
    /// The closed dates as the server last answered them. Each add or
    /// remove saves on its own and this list is replaced by the server's
    /// (parity #7) — never a whole list sent behind Save hours.
    @State private var closures: [String]
    @State private var newClosure = Date()
    @State private var postedLabel: String?
    /// "From Google — not saved yet" / "Monday's hours on every day — not
    /// saved yet": the form changed, nothing is stored until Save hours.
    @State private var fillNote: String?
    @State private var fillError: String?
    @State private var fillingFromGoogle = false
    @State private var pendingRemoval: String?

    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// "Fri, 9/25/26" — M/D/YY, the one owner-facing date form (F3-17). It
    /// read "Fri, Sep 25, 2026".
    static func closureLabel(_ iso: String) -> String {
        guard let date = dayFormatter.date(from: iso) else { return CavnarDate.mdy(iso) }
        let weekday = DateFormatter()
        weekday.locale = Locale(identifier: "en_US_POSIX")
        weekday.dateFormat = "EEE"
        return weekday.string(from: date) + ", " + CavnarDate.mdy(iso)
    }

    init(viewModel: AccountViewModel, profile: AccountProfile) {
        self.viewModel = viewModel
        self.profile = profile
        func decode(_ json: String?) -> [String: String] {
            guard let json, let data = json.data(using: .utf8),
                  let dict = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
            return dict
        }
        _hours = State(initialValue: HoursDayDraft.drafts(days: Self.days, opens: decode(profile.openTimesJson),
                                                          closes: decode(profile.closeTimesJson)))
        // The scheduler's closed dates (Friction #6: these used to land in
        // the marketing holiday list and never reached the scheduler).
        _closures = State(initialValue: (profile.closures ?? []).filter { !$0.isEmpty }.sorted())
    }

    /// Read only for a login the server won't take hours from.
    private var canEdit: Bool { profile.hoursCanEdit != false }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Hours & closures") {
                        GlowBadge(systemImage: "clock", size: 64)
                    } subtitle: {
                        Text("Drives schedules and \"today\"")
                    }

                    if !canEdit {
                        Text("Your login can see these hours, not change them.")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                    }

                    AccountSection(kicker: "Weekly hours") {
                        if let fillNote {
                            Text(fillNote)
                                .font(.cavnarBody(14, weight: 600))
                                .foregroundStyle(Color.cavnarAmber)
                                .padding(.vertical, 6)
                        }
                        if let fillError {
                            Text(fillError)
                                .font(.cavnarBody(14))
                                .foregroundStyle(Color.cavnarRed)
                                .padding(.vertical, 6)
                        }
                        if fillingFromGoogle {
                            CavnarSkeletonBar(height: 3)
                                .padding(.vertical, 6)
                                .accessibilityLabel("Reading your Google hours")
                        }
                        ForEach(Array(Self.days.enumerated()), id: \.element) { index, day in
                            dayRow(day, showsDivider: index < Self.days.count - 1)
                        }
                    }
                    .disabled(!canEdit)

                    if canEdit {
                    Button {
                        Task {
                            // Only what the owner changed is rewritten: an
                            // untouched day goes back exactly as it was stored
                            // (F3-17).
                            let (open, close) = HoursDayDraft.payload(days: Self.days, drafts: hours)
                            if await viewModel.saveHours(open: open, close: close) {
                                Haptic.success()
                                fillNote = nil
                                postedLabel = "Hours saved"
                            }
                        }
                    } label: {
                        Group {
                            if viewModel.isSavingHours { CavnarShimmerText(text: "Saving…") } else { Text("Save hours") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSavingHours))
                    .disabled(viewModel.isSavingHours)
                    }

                    if let error = viewModel.saveHoursError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    // Closed dates save the moment one is added or removed —
                    // no Save step (parity #7, owner edits never vanish).
                    AccountSection(kicker: closures.isEmpty ? "Closed dates" : "Closed dates · \(closures.count)") {
                        ForEach(Array(closures.enumerated()), id: \.element) { index, date in
                            AccountKVRow(label: Self.closureLabel(date)) {
                                if canEdit {
                                    if viewModel.closureBusy == date {
                                        CavnarShimmerLine(color: .cavnarRed).frame(width: 28)
                                    } else {
                                        AccountActionChip(symbol: "xmark", tone: .cavnarRed,
                                                          accessibilityLabel: "Remove \(Self.closureLabel(date))") {
                                            pendingRemoval = date
                                        }
                                    }
                                }
                            }
                        }
                        if canEdit {
                        HStack(spacing: 12) {
                            DatePicker("", selection: $newClosure, displayedComponents: .date)
                                .labelsHidden()
                                .tint(Color.cavnarEmber)
                            Spacer(minLength: 0)
                            if let busy = viewModel.closureBusy, !closures.contains(busy) {
                                CavnarShimmerLine(color: .cavnarEmber).frame(width: 28)
                            } else {
                                AccountActionChip(symbol: "plus", accessibilityLabel: "Add closed date") {
                                    let s = Self.dayFormatter.string(from: newClosure)
                                    guard !closures.contains(s) else { return }
                                    Task { await applyClosure(.init(add: s), saved: "Closed \(CavnarDate.mdy(s)) saved") }
                                }
                            }
                        }
                        .padding(.vertical, 9)
                        }
                        Text("Each date saves as soon as you add or remove it.")
                            .font(.cavnarBody(13.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.bottom, 6)
                    }
                    if let error = viewModel.closureError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Hours")
            .toolbar {
                if canEdit {
                    cavnarToolbarItem(placement: .topBarTrailing) {
                        // Fill the form only: nothing saves until Save hours
                        // (parity #88, the web's Fill from Google / Same
                        // every day).
                        Menu {
                            Button {
                                Task { await fillFromGoogle() }
                            } label: { Label("Fill from Google", systemImage: "globe") }
                            Button {
                                copyMondayToAll()
                            } label: { Label("Copy Monday to all", systemImage: "doc.on.doc") }
                        } label: {
                            Image(systemName: "wand.and.stars")
                                .font(.system(size: 16, weight: .semibold))
                                .foregroundStyle(Color.cavnarEmber)
                                .cavnarToolbarIconGlass()
                        }
                        .accessibilityLabel("Fill hours")
                    }
                }
            }
            .confirmationDialog(
                pendingRemoval.map { "Remove \(Self.closureLabel($0))?" } ?? "",
                isPresented: Binding(get: { pendingRemoval != nil }, set: { if !$0 { pendingRemoval = nil } }),
                titleVisibility: .visible
            ) {
                Button("Remove closed date", role: .destructive) {
                    guard let date = pendingRemoval else { return }
                    Task { await applyClosure(.init(remove: date), saved: "Date removed") }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("The schedule plans this day as open again. It saves right away.")
            }
            .cavnarPostedOverlay(postedLabel) { if postedLabel == "Hours saved" { dismiss() } else { postedLabel = nil } }
        }
    }

    /// Saves one closed date and adopts the list the server answers with.
    private func applyClosure(_ change: AccountViewModel.ClosureChange, saved: String) async {
        if let list = await viewModel.changeClosure(change) {
            closures = list
            Haptic.success()
            postedLabel = saved
        } else {
            Haptic.error()
        }
    }

    /// Google's listed hours into the form; a day Google lists no hours for
    /// is closed. Not saved until Save hours.
    private func fillFromGoogle() async {
        fillError = nil
        fillingFromGoogle = true
        defer { fillingFromGoogle = false }
        let answer = await viewModel.hoursFromGoogle()
        guard answer.ok, let open = answer.open, !open.isEmpty else {
            fillError = answer.error ?? "Your Google listing has no opening hours on it."
            Haptic.error()
            return
        }
        hours = HoursDayDraft.filled(days: Self.days, current: hours, opens: open, closes: answer.close ?? [:])
        fillNote = "From Google \u{2014} not saved yet"
        Haptic.success()
    }

    /// Monday's open and close on every day — the form only.
    private func copyMondayToAll() {
        fillError = nil
        guard let monday = hours["Monday"], !monday.closed else {
            fillError = "Set Monday's hours first \u{2014} it's closed."
            Haptic.error()
            return
        }
        hours = HoursDayDraft.copying(monday, to: Self.days, current: hours)
        fillNote = "Monday\u{2019}s hours on every day \u{2014} not saved yet"
        Haptic.success()
    }

    private func dayRow(_ day: String, showsDivider: Bool) -> some View {
        let binding = Binding<HoursDayDraft>(
            get: { hours[day] ?? HoursDayDraft(originalOpen: nil, originalClose: nil, closed: false) },
            set: { hours[day] = $0 }
        )
        // Two lines: the day and its switch, then the open–close pickers.
        // Two compact DatePickers plus the switch on ONE line left the day
        // name ~60pt, so "Monday" wrapped to "Mon / day".
        return VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 12) {
                    Text(day)
                        .font(.cavnarBody(16, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                        .lineLimit(1)
                        .fixedSize()
                    Spacer(minLength: 8)
                    if binding.wrappedValue.closed {
                        Text("Closed").font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                    }
                    AccountStateSwitch(isOn: Binding(get: { !binding.wrappedValue.closed }, set: { open in
                        binding.wrappedValue.setClosed(!open)
                    }))
                }
                if !binding.wrappedValue.closed {
                    HStack(spacing: 10) {
                        Text("Open").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                        DatePicker("", selection: Binding(get: { binding.wrappedValue.openTime },
                                                          set: { binding.wrappedValue.setOpen($0) }),
                                   displayedComponents: .hourAndMinute).labelsHidden().tint(Color.cavnarEmber)
                        Text("Close").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3).padding(.leading, 6)
                        DatePicker("", selection: Binding(get: { binding.wrappedValue.closeTime },
                                                          set: { binding.wrappedValue.setClose($0) }),
                                   displayedComponents: .hourAndMinute).labelsHidden().tint(Color.cavnarEmber)
                        Spacer(minLength: 0)
                    }
                    // A stored time the app can't read, or a day with only a
                    // close time: said, and left as stored unless changed.
                    if let note = binding.wrappedValue.storedNote {
                        Text(note).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                    }
                }
            }
            .padding(.vertical, 9)
            .animation(.easeOut(duration: 0.2), value: binding.wrappedValue.closed)
            if showsDivider { AccountRowDivider() }
        }
    }
}

/// One day of the Hours sheet, keeping what was STORED apart from what the
/// owner changed (F3-17). The sheet used to read only "h:mma": "11am",
/// "23:00" or an admin-typed string parsed as Closed, and saving then
/// dropped that day's hours; a restaurant with close times only got an
/// invented "11:00am" open on every day. Now a time is read in any of the
/// forms the server holds, an unreadable one is "unknown" (never "closed"),
/// and a day nobody touched is sent back exactly as stored.
struct HoursDayDraft: Equatable {
    let originalOpen: String?
    let originalClose: String?
    private(set) var closed: Bool
    private(set) var openTime: Date
    private(set) var closeTime: Date
    private var openEdited = false
    private var closeEdited = false
    /// Switched from closed to open here: the times shown are the ones
    /// the owner is opening it with.
    private var reopened = false

    init(originalOpen: String?, originalClose: String?, closed: Bool) {
        self.originalOpen = originalOpen
        self.originalClose = originalClose
        self.closed = closed
        openTime = HoursFormat.date(HoursFormat.parse(originalOpen) ?? (11, 0))
        closeTime = HoursFormat.date(HoursFormat.parse(originalClose) ?? (21, 0))
    }

    /// Every day of the week from the stored dictionaries. A day with no
    /// time at all, while other days have some, is closed (the web's rule);
    /// with nothing stored anywhere, every day starts open and unset.
    static func drafts(days: [String], opens: [String: String], closes: [String: String]) -> [String: HoursDayDraft] {
        let anyConfigured = !(opens.isEmpty && closes.isEmpty)
        var out: [String: HoursDayDraft] = [:]
        for day in days {
            let o = opens[day].flatMap { $0.trimmingCharacters(in: .whitespaces).isEmpty ? nil : $0 }
            let c = closes[day].flatMap { $0.trimmingCharacters(in: .whitespaces).isEmpty ? nil : $0 }
            out[day] = HoursDayDraft(originalOpen: o, originalClose: c, closed: anyConfigured && o == nil && c == nil)
        }
        return out
    }

    mutating func setClosed(_ value: Bool) {
        if closed && !value { reopened = true }
        closed = value
    }

    mutating func setOpen(_ date: Date) {
        openTime = date
        openEdited = true
    }

    mutating func setClose(_ date: Date) {
        closeTime = date
        closeEdited = true
    }

    /// What is saved for the open time: nil when closed; the picker's time
    /// when the owner set it (or opened the day here); otherwise exactly
    /// what was stored — including nothing.
    var openValue: String? {
        if closed { return nil }
        if openEdited || reopened { return HoursFormat.format(openTime) }
        return originalOpen
    }

    var closeValue: String? {
        if closed { return nil }
        if closeEdited || reopened { return HoursFormat.format(closeTime) }
        return originalClose
    }

    /// Said under a day whose stored hours the pickers don't show as-is.
    var storedNote: String? {
        if let o = originalOpen, HoursFormat.parse(o) == nil, !openEdited {
            return "Opens \u{201C}\(o)\u{201D} as saved \u{2014} kept unless you change it."
        }
        if let c = originalClose, HoursFormat.parse(c) == nil, !closeEdited {
            return "Closes \u{201C}\(c)\u{201D} as saved \u{2014} kept unless you change it."
        }
        if originalOpen == nil, originalClose != nil, !openEdited, !reopened {
            return "No opening time saved \u{2014} set one to add it."
        }
        return nil
    }

    /// A day set to these times, as if the owner picked them (saved as
    /// picked, not "as stored").
    static func picked(original: HoursDayDraft?, open: (hour: Int, minute: Int), close: (hour: Int, minute: Int)) -> HoursDayDraft {
        var d = HoursDayDraft(originalOpen: original?.originalOpen, originalClose: original?.originalClose,
                              closed: original?.closed ?? false)
        d.setClosed(false)
        d.setOpen(HoursFormat.date(open))
        d.setClose(HoursFormat.date(close))
        return d
    }

    /// The form filled from Google's listed hours: a day it lists is set to
    /// them, a day it lists no hours for is closed, a time it lists that
    /// can't be read leaves that day as it was.
    static func filled(days: [String], current: [String: HoursDayDraft],
                       opens: [String: String], closes: [String: String]) -> [String: HoursDayDraft] {
        var out = current
        for day in days {
            let o = HoursFormat.parse(opens[day]), c = HoursFormat.parse(closes[day])
            if let o, let c {
                out[day] = picked(original: current[day], open: o, close: c)
            } else if opens[day] == nil && closes[day] == nil {
                var d = current[day] ?? HoursDayDraft(originalOpen: nil, originalClose: nil, closed: false)
                d.setClosed(true)
                out[day] = d
            }
        }
        return out
    }

    /// `source`'s times on every day (the web's "Same every day").
    static func copying(_ source: HoursDayDraft, to days: [String],
                        current: [String: HoursDayDraft]) -> [String: HoursDayDraft] {
        let cal = Calendar.current
        let o = cal.dateComponents([.hour, .minute], from: source.openTime)
        let c = cal.dateComponents([.hour, .minute], from: source.closeTime)
        var out = current
        for day in days {
            out[day] = picked(original: current[day], open: (o.hour ?? 11, o.minute ?? 0),
                              close: (c.hour ?? 21, c.minute ?? 0))
        }
        return out
    }

    /// The two dictionaries the save route takes. A day missing from both
    /// is closed.
    static func payload(days: [String], drafts: [String: HoursDayDraft]) -> (open: [String: String], close: [String: String]) {
        var open: [String: String] = [:], close: [String: String] = [:]
        for day in days {
            guard let d = drafts[day] else { continue }
            if let v = d.openValue { open[day] = v }
            if let v = d.closeValue { close[day] = v }
        }
        return (open, close)
    }
}

/// The time strings the server stores: "11:00am", "11am", "11 am",
/// "23:00", "9:30 PM". Written back in the app's own "h:mma" form.
enum HoursFormat {
    static func parse(_ raw: String?) -> (hour: Int, minute: Int)? {
        guard var s = raw?.lowercased().replacingOccurrences(of: " ", with: ""), !s.isEmpty else { return nil }
        s = s.replacingOccurrences(of: ".", with: "")
        var meridiem: String?
        if s.hasSuffix("am") || s.hasSuffix("pm") {
            meridiem = String(s.suffix(2))
            s = String(s.dropLast(2))
        } else if s.hasSuffix("a") || s.hasSuffix("p") {
            meridiem = s.hasSuffix("a") ? "am" : "pm"
            s = String(s.dropLast())
        }
        let parts = s.split(separator: ":", omittingEmptySubsequences: false)
        guard (1...2).contains(parts.count), let h = Int(parts[0]) else { return nil }
        let m = parts.count == 2 ? Int(parts[1]) : 0
        guard let minute = m, (0..<60).contains(minute) else { return nil }
        if let meridiem {
            guard (1...12).contains(h) else { return nil }
            return (meridiem == "am" ? h % 12 : h % 12 + 12, minute)
        }
        guard (0..<24).contains(h), parts.count == 2 else { return nil }
        return (h, minute)
    }

    static func format(_ date: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: date)
        let h = c.hour ?? 0, m = c.minute ?? 0
        return "\(h % 12 == 0 ? 12 : h % 12):\(String(format: "%02d", m))\(h < 12 ? "am" : "pm")"
    }

    static func date(_ t: (hour: Int, minute: Int)) -> Date {
        Calendar.current.date(bySettingHour: t.hour, minute: t.minute, second: 0, of: Date()) ?? Date()
    }
}
