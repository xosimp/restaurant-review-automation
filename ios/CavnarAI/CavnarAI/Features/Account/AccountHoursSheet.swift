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
    @State private var closures: [String]
    @State private var newClosure = Date()
    @State private var postedLabel: String?

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
                        ForEach(Array(Self.days.enumerated()), id: \.element) { index, day in
                            dayRow(day, showsDivider: index < Self.days.count - 1)
                        }
                    }
                    .disabled(!canEdit)

                    AccountSection(kicker: closures.isEmpty ? "Closures" : "Closures · \(closures.count)") {
                        ForEach(Array(closures.enumerated()), id: \.element) { index, date in
                            AccountKVRow(label: Self.closureLabel(date)) {
                                if canEdit {
                                    AccountActionChip(symbol: "xmark", tone: .cavnarRed, accessibilityLabel: "Remove closure") {
                                        closures.removeAll { $0 == date }
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
                            AccountActionChip(symbol: "plus", accessibilityLabel: "Add closure") {
                                let s = Self.dayFormatter.string(from: newClosure)
                                if !closures.contains(s) { closures.append(s); closures.sort() }
                            }
                        }
                        .padding(.vertical, 9)
                        }
                    }

                    if let error = viewModel.saveHoursError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    if canEdit {
                    Button {
                        Task {
                            // Only what the owner changed is rewritten: an
                            // untouched day goes back exactly as it was stored
                            // (F3-17).
                            let (open, close) = HoursDayDraft.payload(days: Self.days, drafts: hours)
                            if await viewModel.saveHours(open: open, close: close, closures: closures) {
                                Haptic.success()
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
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Hours")
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
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
