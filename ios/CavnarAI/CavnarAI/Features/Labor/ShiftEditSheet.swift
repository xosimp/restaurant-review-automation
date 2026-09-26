import SwiftUI

/// Change a shift's times, or put a new shift on the drafted week — the
/// web editor's pencil and "+ Add a shift" (web parity, 9/25/26). Saved the
/// way every edit on the phone is: the week is re-scored and stored through
/// labor/schedule/score (LaborViewModel.rescoreQuality), the same route the
/// web's Save uses. Building a week stays desktop-first; this is for the
/// fix on the floor ("Sam starts at 5, not 4").
struct ShiftEditSheet: View {
    enum Mode: Identifiable {
        case edit(ScheduleRow)
        case add
        var id: String {
            switch self {
            case .edit(let row): return "edit|" + row.id
            case .add: return "add"
            }
        }
    }

    let mode: Mode
    let viewModel: LaborViewModel

    @Environment(\.dismiss) private var dismiss
    @State private var start = Date()
    @State private var end = Date()
    @State private var date = ""
    @State private var employee = ""
    @State private var error: String?
    @State private var saving = false

    private var rows: [ScheduleRow] { viewModel.scheduleResult?.previewRows ?? [] }

    /// The week's dates, in order.
    private var weekDates: [String] {
        Array(Set(rows.compactMap { $0.date.map { String($0.prefix(10)) } })).sorted()
    }

    /// Everyone on the week, with the role they work on it.
    private var people: [String] {
        Array(Set(rows.compactMap { ($0.employee ?? "").isEmpty ? nil : $0.employee })).sorted()
    }

    private func roleOf(_ name: String) -> String? {
        rows.first { ($0.employee ?? "").lowercased() == name.lowercased() && !($0.role ?? "").isEmpty }?.role
    }

    private var title: String {
        switch mode {
        case .edit(let row): return row.employee ?? "Edit shift"
        case .add: return "Add a shift"
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if case .edit(let row) = mode {
                        HomeMixedText.make([row.day ?? "", CavnarDate.mdy(row.date ?? ""), row.role ?? ""]
                                            .filter { !$0.isEmpty }.joined(separator: " · "),
                                           size: 15, weight: 600, color: .cavnarInk2)
                    } else {
                        VStack(spacing: 0) {
                            pickerRow("Day") {
                                Picker("Day", selection: $date) {
                                    ForEach(weekDates, id: \.self) { d in
                                        Text(LaborWaitingOnYou.dayLabel(d)).tag(d)
                                    }
                                }
                            }
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                            pickerRow("Who") {
                                Picker("Who", selection: $employee) {
                                    ForEach(people, id: \.self) { Text($0).tag($0) }
                                }
                            }
                        }
                        .cavnarCard()
                    }
                    VStack(spacing: 0) {
                        pickerRow("Starts") {
                            DatePicker("Starts", selection: $start, displayedComponents: .hourAndMinute)
                                .labelsHidden()
                        }
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        pickerRow("Ends") {
                            DatePicker("Ends", selection: $end, displayedComponents: .hourAndMinute)
                                .labelsHidden()
                        }
                    }
                    .cavnarCard()
                    if let hours = LaborViewModel.shiftHours(timeText(start), timeText(end)) {
                        HomeMixedText.make("\(hours) hours · the week is re-scored and saved when you tap Save.",
                                           size: 13.5, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let error {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }
                    VStack(spacing: 10) {
                        Button {
                            Task { await save() }
                        } label: {
                            Group {
                                if saving { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save") }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: saving))
                        .disabled(saving)
                        Button { dismiss() } label: { Text("Cancel").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar(title) }
        }
        .onAppear(perform: seed)
    }

    private func pickerRow<Control: View>(_ label: String, @ViewBuilder control: () -> Control) -> some View {
        HStack {
            Text(label).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
            Spacer(minLength: 8)
            control().tint(Color.cavnarEmber)
        }
        .frame(minHeight: 48)
    }

    /// The shift's own times; for a new one, the first shift's (the web's
    /// default is the row being looked at, else the first — Friction #44).
    private func seed() {
        switch mode {
        case .edit(let row):
            start = dateFor(row.shiftStart) ?? dateFor("4:00pm")!
            end = dateFor(row.shiftEnd) ?? dateFor("10:00pm")!
        case .add:
            let first = rows.first
            date = weekDates.first ?? ""
            employee = people.first ?? ""
            start = dateFor(first?.shiftStart) ?? dateFor("4:00pm")!
            end = dateFor(first?.shiftEnd) ?? dateFor("10:00pm")!
        }
    }

    private func dateFor(_ text: String?) -> Date? {
        guard let m = LaborViewModel.shiftMinutes(text) else { return nil }
        return Calendar.current.date(bySettingHour: m / 60, minute: m % 60, second: 0, of: Date())
    }

    private func timeText(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: d)
        return LaborViewModel.shiftTimeText(minutes: (c.hour ?? 0) * 60 + (c.minute ?? 0))
    }

    private func save() async {
        error = nil
        saving = true
        defer { saving = false }
        let s = timeText(start), e = timeText(end)
        guard s != e else {
            error = "Start and end can\u{2019}t be the same time."
            return
        }
        switch mode {
        case .edit(let row):
            if let refusal = await viewModel.editShiftTimes(rowId: row.id, start: s, end: e) {
                error = refusal
            } else {
                dismiss()
            }
        case .add:
            guard !date.isEmpty else { error = "Pick a day."; return }
            if let refusal = await viewModel.addShift(date: date, employee: employee, role: roleOf(employee),
                                                      start: s, end: e) {
                error = refusal
            } else {
                dismiss()
            }
        }
    }
}
