import SwiftUI

// The person sheet's new facts (schedule audit 10/3/26): who runs the floor
// and who stands in, the shifts somebody always works, training, the roles
// a closer closes for, attendance with lateness and call-outs, and the
// "not worked since" question. Each is a section in the Account kit, saved
// the moment it changes (an add or a remove is one save), through the same
// POST labor/staff-settings the rest of the sheet uses.

/// A choice as a menu on a row — the kit's answer to a select.
struct SetupSelectMenu: View {
    let current: String
    let options: [(value: String, label: String)]
    var disabled: Bool = false
    let onPick: (String) -> Void

    var body: some View {
        Menu {
            ForEach(options, id: \.value) { option in
                Button {
                    Haptic.selection()
                    onPick(option.value)
                } label: { Text(option.label) }
            }
        } label: {
            HStack(spacing: 6) {
                Text(current)
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .padding(.horizontal, 10)
            .frame(minHeight: 34)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper2))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        }
        .disabled(disabled)
        .opacity(disabled ? 0.6 : 1)
    }
}

/// One choice among a few, as the roster sheet's chips draw them.
struct SetupChoiceChip: View {
    let label: String
    let on: Bool
    var enabled: Bool = true
    var tone: Color = .cavnarEmber
    let action: () -> Void

    var body: some View {
        Button {
            guard enabled, !on else { return }
            Haptic.selection()
            action()
        } label: {
            Text(label)
                .font(.cavnarBody(CavnarType.caption, weight: on ? 700 : 500))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 32)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(on ? tone : Color.cavnarPaper2))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
                .opacity(enabled ? 1 : 0.6)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// A helper line under a section, the kit's 13pt ink3.
struct SetupHelp: View {
    let text: String
    var color: Color = .cavnarInk3
    var body: some View {
        HomeMixedText.make(text, size: CavnarType.caption, color: color)
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Not worked since … (F2, E-3)

/// "Not worked since 8/14/26 — deactivate?" with the two answers: take
/// them off the roster, or keep them (any settings save keeps them for six
/// more weeks).
struct RosterDormantNotice: View {
    let text: String
    let busy: Bool
    let editable: Bool
    let onDeactivate: () -> Void
    let onStillHere: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "moon.zzz")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.top, 2)
                HomeMixedText.make(text, size: CavnarType.secondary, weight: 600, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Left off every draft until you answer \u{2014} Cavnar AI never chooses them, and open-shift notices skip them.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if editable {
                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        onDeactivate()
                    } label: { Text("Deactivate").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
                        .disabled(busy)
                    Button {
                        Haptic.light()
                        onStillHere()
                    } label: { Text("Still here").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
                        .disabled(busy)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarAmber.opacity(0.08)))
    }
}

// MARK: - Runs the floor (F1, P-7 / E-14 / E-13)

/// Floor manager yes / no / automatic, with why automatic reads them the
/// way it does — the account holder's alone.
struct FloorManagerSection: View {
    let status: FloorManagerStatus?
    let stored: Bool?
    let canEdit: Bool
    let busy: Bool
    let onChoose: (ScheduleSetupViewModel.StaffSettingsPatch.FloorManagerChoice) -> Void

    private var choice: ScheduleSetupViewModel.StaffSettingsPatch.FloorManagerChoice {
        switch stored ?? status?.set {
        case true?: return .yes
        case false?: return .no
        case nil: return .automatic
        }
    }

    private var whyLine: String {
        let counts = status?.counts ?? false
        let why = status?.why.flatMap { $0.isEmpty ? nil : $0 }
        switch choice {
        case .yes: return "Counts as a manager on the floor \u{2014} you set them as one."
        case .no: return "Never counted as a manager on the floor \u{2014} you set them as not."
        case .automatic:
            if counts { return "Automatic: counts as a manager" + (why.map { " \u{2014} \($0)" } ?? "") + "." }
            return "Automatic: not counted as a manager" + (why.map { " \u{2014} \($0)" } ?? "") + "."
        }
    }

    var body: some View {
        AccountSection(kicker: "Runs the floor") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    SetupChoiceChip(label: "Yes", on: choice == .yes, enabled: canEdit && !busy) { onChoose(.yes) }
                    SetupChoiceChip(label: "No", on: choice == .no, enabled: canEdit && !busy, tone: .cavnarInk3) { onChoose(.no) }
                    SetupChoiceChip(label: "Automatic", on: choice == .automatic, enabled: canEdit && !busy,
                                    tone: .cavnarInk2) { onChoose(.automatic) }
                }
                HomeMixedText.make(whyLine, size: CavnarType.secondary, weight: 500,
                                   color: (status?.counts ?? false) || choice == .yes ? .cavnarInk2 : .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                Text(canEdit
                     ? "A manager or owner is on the floor every minute anyone is \u{2014} this says who counts."
                     : "Only the account owner sets who runs the floor.")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 10)
        }
    }
}

/// The dates somebody stands in as the manager on duty — counted as the
/// manager on those dates only. Each add or remove saves at once.
struct ActingManagerSection: View {
    let ranges: [ActingRange]
    let canEdit: Bool
    let busy: Bool
    let onSave: ([ActingRange]) -> Void

    @State private var from = ""
    @State private var until = ""
    @State private var note = ""

    private var today: String { CavnarDate.isoDay(Date()) }

    var body: some View {
        AccountSection(kicker: "Stands in as the manager") {
            VStack(alignment: .leading, spacing: 0) {
                if ranges.isEmpty {
                    Text("No dates \u{2014} they count as a manager only if they run the floor anyway.")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.vertical, 10)
                } else {
                    ForEach(Array(ranges.enumerated()), id: \.element) { index, range in
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 2) {
                                HomeMixedText.make(range.label, size: CavnarType.body, weight: 600, color: .cavnarInk)
                                if let note = range.note, !note.isEmpty {
                                    Text(note).font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                                }
                            }
                            Spacer(minLength: 8)
                            if canEdit {
                                AccountActionChip(symbol: "xmark", tone: .cavnarRed,
                                                  accessibilityLabel: "Remove \(range.label)") {
                                    onSave(ranges.filter { $0 != range })
                                }
                                .disabled(busy)
                            }
                        }
                        .padding(.vertical, 9)
                        if index < ranges.count - 1 { AccountRowDivider() }
                    }
                }
                if canEdit {
                    AccountRowDivider()
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(spacing: 8) {
                            Text("From").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                            CavnarDateChip(iso: $from, accessibilityName: "Acting from")
                            Text("until").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                            CavnarDateChip(iso: $until, earliest: CavnarDateChip.day(from), accessibilityName: "Acting until")
                            Spacer(minLength: 0)
                        }
                        TextField("Note (optional) \u{2014} e.g. Erik on vacation", text: $note)
                            .cavnarTextFieldStyle()
                        Button {
                            let start = from.isEmpty ? today : from
                            let end = until.isEmpty || until < start ? start : until
                            let n = note.trimmingCharacters(in: .whitespacesAndNewlines)
                            var next = ranges
                            next.append(ActingRange(from: start, until: end, note: n.isEmpty ? nil : n))
                            onSave(next.sorted { ($0.from, $0.until) < ($1.from, $1.until) })
                            from = ""; until = ""; note = ""
                        } label: {
                            Text("Add these dates").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy || from.isEmpty))
                        .disabled(busy || from.isEmpty)
                    }
                    .padding(.vertical, 10)
                } else {
                    SetupHelp(text: "Only the account owner sets who stands in as the manager.")
                        .padding(.vertical, 8)
                }
            }
        }
    }
}

// MARK: - Always works (F1, D-5)

/// The shifts somebody always works — the owners' and managers' real floor
/// days the manager plan starts from. Each add or remove saves at once.
struct StandingShiftsSection: View {
    let shifts: [StandingShift]
    let roles: [String]
    let canEdit: Bool
    let busy: Bool
    let onSave: ([StandingShift]) -> Void

    @State private var day = "Monday"
    @State private var start = "10:00am"
    @State private var end = "6:00pm"
    @State private var role = ""
    @State private var limited = false
    @State private var from = ""
    @State private var until = ""

    private static let days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    var body: some View {
        AccountSection(kicker: "Always works") {
            VStack(alignment: .leading, spacing: 0) {
                if shifts.isEmpty {
                    Text("No standing shifts. Add the days and hours they always work \u{2014} every draft keeps them.")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.vertical, 10)
                } else {
                    ForEach(Array(shifts.enumerated()), id: \.element.id) { index, shift in
                        HStack(spacing: 10) {
                            HomeMixedText.make(shift.line, size: CavnarType.body, weight: 600, color: .cavnarInk)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 8)
                            if canEdit {
                                AccountActionChip(symbol: "xmark", tone: .cavnarRed,
                                                  accessibilityLabel: "Remove \(shift.line)") {
                                    onSave(shifts.filter { $0 != shift })
                                }
                                .disabled(busy)
                            }
                        }
                        .padding(.vertical, 9)
                        if index < shifts.count - 1 { AccountRowDivider() }
                    }
                }
                if canEdit {
                    AccountRowDivider()
                    addForm.padding(.vertical, 10)
                }
            }
        }
    }

    private var addForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            AccountFlowLayout(spacing: 6) {
                ForEach(Self.days, id: \.self) { d in
                    Button {
                        Haptic.selection()
                        day = d
                    } label: { AccountChip(text: String(d.prefix(3)), muted: day != d) }
                        .buttonStyle(.plain)
                        .accessibilityLabel(d)
                        .accessibilityAddTraits(day == d ? .isSelected : [])
                }
            }
            HStack(spacing: 8) {
                CavnarTimeChip(time: $start, accessibilityName: "Starts")
                Text("to").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                CavnarTimeChip(time: $end, accessibilityName: "Ends")
                Spacer(minLength: 0)
            }
            HStack(spacing: 8) {
                Text("Role").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                SetupSelectMenu(current: role.isEmpty ? "Their usual role" : role,
                                options: [("", "Their usual role")] + roles.map { ($0, $0) }) { role = $0 }
                Spacer(minLength: 0)
            }
            Button {
                Haptic.selection()
                limited.toggle()
                if !limited { from = ""; until = "" }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: limited ? "checkmark.square.fill" : "square")
                        .foregroundStyle(limited ? Color.cavnarEmber2 : Color.cavnarInk3)
                    Text("Only between dates").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2)
                }
            }
            .buttonStyle(.plain)
            if limited {
                HStack(spacing: 8) {
                    CavnarDateChip(iso: $from, accessibilityName: "From")
                    Text("until").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                    CavnarDateChip(iso: $until, earliest: CavnarDateChip.day(from), accessibilityName: "Until")
                    Spacer(minLength: 0)
                }
            }
            Button {
                var next = shifts
                next.append(StandingShift(day: day, start: start, end: end, role: role.isEmpty ? nil : role,
                                          from: limited && !from.isEmpty ? from : nil,
                                          until: limited && !until.isEmpty ? until : nil))
                onSave(next)
                limited = false; from = ""; until = ""
            } label: {
                Text("Add \(day)s").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
            .disabled(busy)
        }
    }
}

// MARK: - In training (F1, D-16)

/// In training for a role: their shifts in it are never coverage, and the
/// draft pairs them with the trainer. Until is required — a trainee with
/// no end stayed one for good.
struct TraineeSection: View {
    let trainee: TraineeInfo?
    let name: String
    let roles: [String]
    let trainers: [String]
    let canEdit: Bool
    let busy: Bool
    let onSave: (ScheduleSetupViewModel.StaffSettingsPatch.TraineeChange) -> Void

    @State private var role = ""
    @State private var trainer = ""
    @State private var from = ""
    @State private var until = ""

    private var defaultUntil: String {
        CavnarDate.isoDay(Calendar.current.date(byAdding: .day, value: 28, to: Date()) ?? Date())
    }

    var body: some View {
        AccountSection(kicker: "In training") {
            VStack(alignment: .leading, spacing: 10) {
                if let t = trainee {
                    HStack(alignment: .top, spacing: 10) {
                        AccountChip(text: "Training", muted: true)
                        HomeMixedText.make(t.line, size: CavnarType.body, weight: 600, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    SetupHelp(text: "Their \(t.targetRole) shifts don\u{2019}t count toward who\u{2019}s on, and the draft "
                              + "puts \(t.trainer ?? "a \(t.targetRole)") on beside them. After "
                              + CavnarDate.mdy(t.until) + " they count again.")
                    if canEdit {
                        Button {
                            Haptic.light()
                            onSave(.end)
                        } label: {
                            Text("End training")
                                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                                .frame(minHeight: 36)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(busy)
                    }
                } else if canEdit {
                    SetupHelp(text: "Put \(name) in training for a role: those shifts won\u{2019}t count toward who\u{2019}s on, "
                              + "and the draft pairs them with their trainer.")
                    HStack(spacing: 8) {
                        Text("Learning").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                        SetupSelectMenu(current: role.isEmpty ? "Pick a role" : role,
                                        options: roles.map { ($0, $0) }) { role = $0 }
                        Spacer(minLength: 0)
                    }
                    HStack(spacing: 8) {
                        Text("Trainer").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                        SetupSelectMenu(current: trainer.isEmpty ? "Anyone in the role" : trainer,
                                        options: [("", "Anyone in the role")] + trainers.filter { $0 != name }.map { ($0, $0) }) {
                            trainer = $0
                        }
                        Spacer(minLength: 0)
                    }
                    HStack(spacing: 8) {
                        Text("Until").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                        CavnarDateChip(iso: Binding(get: { until.isEmpty ? defaultUntil : until }, set: { until = $0 }),
                                       earliest: Date(), accessibilityName: "Training ends")
                        Text("from").font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk3)
                        CavnarDateChip(iso: $from, accessibilityName: "Training starts")
                        Spacer(minLength: 0)
                    }
                    Button {
                        let end = until.isEmpty ? defaultUntil : until
                        onSave(.set(TraineeInfo(targetRole: role, trainer: trainer.isEmpty ? nil : trainer,
                                                from: from.isEmpty ? nil : from, until: end)))
                    } label: {
                        Text("Start training").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy || role.isEmpty))
                    .disabled(busy || role.isEmpty)
                } else {
                    Text("Not in training.")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .padding(.vertical, 10)
        }
    }
}

// MARK: - Closes for (F1, D-9)

/// The roles a closer closes for — none picked is their own role. A flag
/// set through Cavnar AI support waits until the owner counts it as theirs.
struct ClosesForSection: View {
    let role: String?
    let closesFor: [String]
    let roles: [String]
    let pending: Bool
    let canEdit: Bool
    let busy: Bool
    let onSave: ([String]) -> Void

    var body: some View {
        AccountSection(kicker: "Closes for") {
            VStack(alignment: .leading, spacing: 8) {
                if pending {
                    SetupHelp(text: "Marked to close through Cavnar AI support \u{2014} it doesn\u{2019}t count until you count "
                              + "it as yours, in Roster & rules \u{2192} Closers.", color: .cavnarAmber)
                }
                Text(closesFor.isEmpty
                     ? "Their own role\(role.map { " (\($0))" } ?? "") \u{2014} pick roles to close for others."
                     : "Closes for \(closesFor.joined(separator: ", ")).")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                AccountFlowLayout(spacing: 6) {
                    ForEach(roles, id: \.self) { r in
                        let on = closesFor.contains { $0.caseInsensitiveCompare(r) == .orderedSame }
                        Button {
                            guard canEdit, !busy else { return }
                            Haptic.selection()
                            onSave(on ? closesFor.filter { $0.caseInsensitiveCompare(r) != .orderedSame } : closesFor + [r])
                        } label: { AccountChip(text: r, muted: !on) }
                            .buttonStyle(.plain)
                            .disabled(!canEdit || busy)
                            .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
                SetupHelp(text: "A closer stays until close and is the last of their role to leave.")
            }
            .padding(.vertical, 10)
        }
    }
}

// MARK: - Attendance (G, D-44 / L-18)

/// What the record says about turning up: missed shifts with the
/// call-outs among them, and lateness over the shifts somebody clocked —
/// the rate "—" until there are six of them. "Not watched yet" when nobody
/// watched, never a clean record.
struct PersonAttendanceSection: View {
    let reliability: RosterReliability?

    var body: some View {
        AccountSection(kicker: "Attendance") {
            if let r = reliability, let missed = r.missedLine {
                AccountKVRow(label: "Missed", showsDivider: r.lateLine != nil) {
                    HomeMixedText.make(missed.replacingOccurrences(of: "Missed ", with: ""), size: CavnarType.body, weight: 600,
                                       color: r.isUnreliable ? .cavnarAmber : .cavnarInk)
                        .multilineTextAlignment(.trailing)
                }
                if let late = r.lateLine {
                    AccountKVRow(label: "Late", showsDivider: false) {
                        HomeMixedText.make(late.replacingOccurrences(of: "Late to ", with: ""), size: CavnarType.body, weight: 600,
                                           color: r.lateRisk == true ? .cavnarAmber : .cavnarInk)
                            .multilineTextAlignment(.trailing)
                    }
                }
                Text("From the shifts Cavnar AI watched. A call-out with notice weighs less than a no-show.")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 9)
            } else {
                Text("Not watched yet \u{2014} attendance is read from shifts Cavnar AI watched, at least six of them.")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 10)
            }
        }
    }
}
