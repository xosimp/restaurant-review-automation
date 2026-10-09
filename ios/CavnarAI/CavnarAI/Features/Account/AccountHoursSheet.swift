import SwiftUI

/// Account -> Profile -> "Hours & closed dates". The week's open/close
/// times (read here, set on the web — iOS readability round) and the dates
/// you're closed (added and removed here, each saved at once). Close times
/// drive schedule generation (the hard cap on a generated shift's end);
/// open times and closures feed the same place.
struct AccountHoursSheet: View {
    let viewModel: AccountViewModel
    let profile: AccountProfile

    private static let days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    @State private var hours: [String: HoursDayDraft]
    /// The closed dates as the server last answered them. Each add or
    /// remove saves on its own and this list is replaced by the server's
    /// (parity #7) — never a whole list sent behind Save hours.
    @State private var closures: [String]
    @State private var newClosure = Date()
    @State private var postedLabel: String?
    @State private var pendingRemoval: String?

    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// "2026-10-08" — the closed-date list's own form.
    static func isoDay(_ date: Date) -> String { dayFormatter.string(from: date) }

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
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    AccountHero(title: "Hours & closed dates") {
                        GlowBadge(systemImage: "clock", size: 64)
                    } subtitle: {
                        Text("Drives schedules and \"today\"")
                    }

                    if !canEdit {
                        Text("Your login can see these hours, not change them.")
                            .cavnarText(.secondary)
                    }

                    // The week's hours, read here and set on the web (iOS
                    // readability round): seven days of time pickers behind
                    // a Save that Back silently discarded.
                    AccountSection(kicker: "Weekly hours") {
                        ForEach(Self.days, id: \.self) { day in
                            AccountKVRow(label: day) {
                                HomeMixedText.make(Self.hoursLine(hours[day]), role: .secondary)
                                    .multilineTextAlignment(.trailing)
                            }
                        }
                        CavnarWebLinkRow(title: "Weekly hours", path: "account/restaurant",
                                         actionLabel: canEdit ? "Edit on the web" : "See on the web")
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
                            .cavnarText(.caption)
                            .padding(.bottom, 6)
                    }
                    if let error = viewModel.closureError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Hours")
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
            .cavnarPostedOverlay(postedLabel) { postedLabel = nil }
        }
    }

    /// "11am–9pm", "Closed", "Not set", or what's stored when the app can't
    /// read it ("Opens “late”") — one day of the week, read only.
    static func hoursLine(_ day: HoursDayDraft?) -> String {
        guard let day else { return "Not set" }
        if day.closed { return "Closed" }
        func clock(_ raw: String?) -> String? {
            guard let raw else { return nil }
            guard let t = HoursFormat.parse(raw) else { return "\u{201C}\(raw)\u{201D}" }
            return HoursFormat.format(HoursFormat.date(t)).replacingOccurrences(of: ":00", with: "")
        }
        switch (clock(day.originalOpen), clock(day.originalClose)) {
        case let (o?, c?): return "\(o)\u{2013}\(c)"
        case let (nil, c?): return "Closes \(c)"
        case let (o?, nil): return "Opens \(o)"
        default: return "Not set"
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
