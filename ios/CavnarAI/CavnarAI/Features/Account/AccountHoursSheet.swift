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

    private struct DayHours {
        var closed: Bool
        var open: Date
        var close: Date
    }

    @State private var hours: [String: DayHours]
    @State private var closures: [String]
    @State private var newClosure = Date()
    @State private var postedLabel: String?

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "h:mma"
        f.amSymbol = "am"; f.pmSymbol = "pm"
        return f
    }()
    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
    private static let displayDayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "EEE, MMM d, yyyy"
        return f
    }()

    private static func parseTime(_ raw: String?, fallbackHour: Int) -> (Date, Bool) {
        let base = Calendar.current.date(bySettingHour: fallbackHour, minute: 0, second: 0, of: Date())!
        guard let raw, !raw.isEmpty else { return (base, false) }
        let cleaned = raw.lowercased().replacingOccurrences(of: " ", with: "")
        if let t = timeFormatter.date(from: cleaned) {
            let c = Calendar.current.dateComponents([.hour, .minute], from: t)
            return (Calendar.current.date(bySettingHour: c.hour ?? fallbackHour, minute: c.minute ?? 0, second: 0, of: Date())!, true)
        }
        return (base, false)
    }

    init(viewModel: AccountViewModel, profile: AccountProfile) {
        self.viewModel = viewModel
        self.profile = profile
        func decode(_ json: String?) -> [String: String] {
            guard let json, let data = json.data(using: .utf8),
                  let dict = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
            return dict
        }
        let opens = decode(profile.openTimesJson)
        let closes = decode(profile.closeTimesJson)
        var h: [String: DayHours] = [:]
        for day in Self.days {
            let (o, hasOpen) = Self.parseTime(opens[day], fallbackHour: 11)
            let (c, hasClose) = Self.parseTime(closes[day], fallbackHour: 21)
            // A day with neither time set is treated as "not configured",
            // shown open with defaults; only an explicit empty is "closed".
            h[day] = DayHours(closed: !(hasOpen || hasClose) && !(opens.isEmpty && closes.isEmpty), open: o, close: c)
        }
        _hours = State(initialValue: h)
        _closures = State(initialValue: (profile.skipHolidays ?? "").split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty })
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Hours & closures") {
                        GlowBadge(systemImage: "clock", size: 64)
                    } subtitle: {
                        Text("Drives schedules and \"today\"")
                    }

                    AccountSection(kicker: "Weekly hours") {
                        ForEach(Array(Self.days.enumerated()), id: \.element) { index, day in
                            dayRow(day, showsDivider: index < Self.days.count - 1)
                        }
                    }

                    AccountSection(kicker: closures.isEmpty ? "Closures" : "Closures · \(closures.count)") {
                        ForEach(Array(closures.enumerated()), id: \.element) { index, date in
                            AccountKVRow(label: Self.dayFormatter.date(from: date).map { Self.displayDayFormatter.string(from: $0) } ?? date) {
                                AccountActionChip(symbol: "xmark", tone: .cavnarRed, accessibilityLabel: "Remove closure") {
                                    closures.removeAll { $0 == date }
                                }
                            }
                        }
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

                    if let error = viewModel.saveHoursError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        Task {
                            var open: [String: String] = [:], close: [String: String] = [:]
                            for day in Self.days {
                                guard let h = hours[day], !h.closed else { continue }
                                open[day] = Self.timeFormatter.string(from: h.open)
                                close[day] = Self.timeFormatter.string(from: h.close)
                            }
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
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Hours")
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }

    private func dayRow(_ day: String, showsDivider: Bool) -> some View {
        let binding = Binding<DayHours>(
            get: { hours[day] ?? DayHours(closed: false, open: Date(), close: Date()) },
            set: { hours[day] = $0 }
        )
        return VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text(day).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 8)
                if binding.wrappedValue.closed {
                    Text("Closed").font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                } else {
                    DatePicker("", selection: binding.open, displayedComponents: .hourAndMinute).labelsHidden().tint(Color.cavnarEmber)
                    Text("–").foregroundStyle(Color.cavnarInk3)
                    DatePicker("", selection: binding.close, displayedComponents: .hourAndMinute).labelsHidden().tint(Color.cavnarEmber)
                }
                AccountStateSwitch(isOn: Binding(get: { !binding.wrappedValue.closed }, set: { open in
                    binding.wrappedValue.closed = !open
                }))
            }
            .padding(.vertical, 9)
            if showsDivider { AccountRowDivider() }
        }
    }
}
