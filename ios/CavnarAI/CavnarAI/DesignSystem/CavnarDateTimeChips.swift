import SwiftUI

/// A date the owner picks, shown the house way — M/D/YY in the number face
/// on a Paper2 control (DESIGN_SYSTEM → Forms, "Dates and times on iOS").
/// The system's compact DatePicker prints the locale's "Oct 3, 2026";
/// this reads `10/3/26`, and a tap opens the graphical calendar in a
/// popover, where nothing is printed as a date. `date` is an ISO day
/// ("2026-10-03") so it goes to the server exactly as picked.
struct CavnarDateChip: View {
    @Binding var iso: String
    /// The earliest day that may be picked (nil: any).
    var earliest: Date? = nil
    var accessibilityName: String = "Date"
    var disabled: Bool = false

    @State private var picking = false

    private var date: Date {
        Self.day(iso) ?? Calendar.current.startOfDay(for: Date())
    }

    var body: some View {
        Button {
            guard !disabled else { return }
            Haptic.light()
            picking = true
        } label: {
            HStack(spacing: 6) {
                Text(iso.isEmpty ? "\u{2014}" : CavnarDate.mdy(iso))
                    .font(.cavnarNumber(15, weight: 700))
                    .foregroundStyle(iso.isEmpty ? Color.cavnarInk3 : Color.cavnarInk)
                Image(systemName: "calendar")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .padding(.horizontal, 10)
            .frame(minHeight: 34)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper2))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(picking ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
            .opacity(disabled ? 0.6 : 1)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(accessibilityName), \(iso.isEmpty ? "not set" : CavnarDate.mdy(iso))")
        .popover(isPresented: $picking) {
            VStack(spacing: 8) {
                calendar
                    .datePickerStyle(.graphical)
                    .labelsHidden()
                    .tint(Color.cavnarEmber)
                Button {
                    picking = false
                } label: {
                    Text("Done").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
            .padding(14)
            .frame(width: 330)
            .presentationCompactAdaptation(.popover)
        }
    }

    @ViewBuilder
    private var calendar: some View {
        let binding = Binding<Date>(get: { date }, set: { iso = CavnarDate.isoDay($0) })
        if let earliest {
            DatePicker("", selection: binding, in: Calendar.current.startOfDay(for: earliest)..., displayedComponents: .date)
        } else {
            DatePicker("", selection: binding, displayedComponents: .date)
        }
    }

    /// The local calendar day an ISO date names, at its start.
    static func day(_ iso: String) -> Date? {
        let p = iso.prefix(10).split(separator: "-")
        guard p.count == 3, let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]) else { return nil }
        return Calendar.current.date(from: DateComponents(year: y, month: m, day: d))
    }
}

/// A time of day the owner picks, in the house form ("6:00pm"): the
/// quarter hours of the day on a wheel in a popover — the iOS twin of the
/// web's 15-minute select (`cavTimeOptions`), never the locale's "6:00 PM".
struct CavnarTimeChip: View {
    @Binding var time: String
    var accessibilityName: String = "Time"
    var disabled: Bool = false

    @State private var picking = false

    var body: some View {
        Button {
            guard !disabled else { return }
            Haptic.light()
            if time.isEmpty { time = "10:00am" }
            picking = true
        } label: {
            Text(time.isEmpty ? "\u{2014}" : time)
                .font(.cavnarNumber(15, weight: 700))
                .foregroundStyle(time.isEmpty ? Color.cavnarInk3 : Color.cavnarInk)
                .padding(.horizontal, 10)
                .frame(minWidth: 76, minHeight: 34)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper2))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(picking ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
                .opacity(disabled ? 0.6 : 1)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(accessibilityName), \(time.isEmpty ? "not set" : time)")
        .popover(isPresented: $picking) {
            VStack(spacing: 8) {
                Picker(accessibilityName, selection: Binding(
                    get: { Self.nearest(time) },
                    set: { time = $0 })) {
                    ForEach(SetupWords.quarterHours, id: \.self) { t in
                        Text(t).font(.cavnarNumber(17, weight: 600)).tag(t)
                    }
                }
                .pickerStyle(.wheel)
                .labelsHidden()
                Button {
                    picking = false
                } label: {
                    Text("Done").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
            .padding(14)
            .frame(width: 260)
            .presentationCompactAdaptation(.popover)
        }
    }

    /// The quarter hour a stored time falls on, so a "6:10pm" typed on the
    /// web still has a row to sit on.
    static func nearest(_ raw: String) -> String {
        guard let m = SetupWords.minutes(raw) else { return "10:00am" }
        return SetupWords.time(minutes: (m / 15) * 15)
    }
}
