import SwiftUI

// The rules sheet's confirmations (schedule audit 10/3/26 F1, A2, F2): who
// runs the floor, the closing setup, the salaried cap and the salaried
// list, and each staffing rule as the schedule checks it. Each section
// reads TeamSetupStore, filled from the same GET labor/rules.

// MARK: - Who runs the floor (F1-1)

/// "Managers: Erik (Owner), Jim (Owner), Anthony (Manager FOH)" — who
/// counts as the manager on the floor next week and why, who was left out
/// and why, who stands in on which dates, and the managers whose working
/// days are nowhere on file. Each name is the account holder's to change.
struct RulesManagersSection: View {
    @Bindable var store: TeamSetupStore
    let canEdit: Bool
    let onChanged: () -> Void
    let onOpenPerson: (String) -> Void

    var body: some View {
        if let m = store.managers {
            AccountSection(kicker: "Who runs the floor") {
                VStack(alignment: .leading, spacing: 0) {
                    HomeMixedText.make(store.managersLine ?? m.line ?? "Managers: nobody yet", size: 15, weight: 600,
                                       color: m.managers.isEmpty ? .cavnarAmber : .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.vertical, 10)
                    ForEach(m.managers) { manager in
                        AccountRowDivider()
                        personRow(name: manager.name, role: manager.role, why: manager.why, counts: true,
                                  ownSetting: manager.basis == "set",
                                  extra: manager.standingShifts.isEmpty ? nil
                                    : "Always works: " + manager.standingShifts.map { "\($0.day.prefix(3)) \($0.start)\u{2013}\($0.end)" }
                                        .joined(separator: ", "))
                    }
                    if !m.notCounted.isEmpty {
                        AccountRowDivider()
                        Text("NOT COUNTED")
                            .font(.cavnarBody(11.5, weight: 700))
                            .tracking(1.1)
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.top, 10)
                        ForEach(m.notCounted) { person in
                            personRow(name: person.name, role: person.role, why: person.why, counts: false,
                                      ownSetting: person.basis == "set_not", extra: nil)
                        }
                    }
                    if !m.acting.isEmpty {
                        AccountRowDivider()
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(m.acting) { a in
                                HomeMixedText.make("\(a.name) stands in" + (a.label.map { " on \($0)" } ?? ""),
                                                   size: 13.5, color: .cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(.vertical, 10)
                    }
                    if let error = store.managerError {
                        Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.bottom, 8)
                    }
                    if !canEdit {
                        SetupHelp(text: "Only the account owner sets who runs the floor.")
                            .padding(.bottom, 10)
                    }
                }
            }
            if let ask = m.askStanding, !m.missingStanding.isEmpty {
                askStandingCard(ask, names: m.missingStanding)
            }
        }
    }

    private func personRow(name: String, role: String?, why: String?, counts: Bool, ownSetting: Bool,
                           extra: String?) -> some View {
        let busy = store.managerBusy == name
        return VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(name + (role.map { " (\($0))" } ?? ""))
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(counts ? Color.cavnarInk : Color.cavnarInk2)
                Spacer(minLength: 6)
                if canEdit {
                    if busy {
                        CavnarShimmerLine(color: .cavnarEmber).frame(width: 28)
                    } else {
                        Menu {
                            if counts {
                                Button("Not a manager") { choose(name, .no) }
                            } else {
                                Button("Counts as a manager") { choose(name, .yes) }
                            }
                            if ownSetting {
                                Button("Back to automatic") { choose(name, .automatic) }
                            }
                        } label: {
                            Text("Change")
                                .font(.cavnarBody(13.5, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                                .frame(minHeight: 32)
                        }
                    }
                }
            }
            if let why, !why.isEmpty {
                HomeMixedText.make(why.prefix(1).uppercased() + why.dropFirst(), size: 13, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let extra {
                HomeMixedText.make(extra, size: 13, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 9)
    }

    private func choose(_ name: String, _ choice: ScheduleSetupViewModel.StaffSettingsPatch.FloorManagerChoice) {
        Task {
            if await store.setFloorManager(name, choice) { onChanged() }
        }
    }

    /// "Which days and hours do Erik and Jim work?" — the manager plan
    /// starts from their standing shifts; each name opens their sheet.
    private func askStandingCard(_ ask: String, names: [String]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HomeMixedText.make(ask, size: 14.5, weight: 600, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            AccountFlowLayout(spacing: 8) {
                ForEach(names, id: \.self) { name in
                    Button {
                        Haptic.light()
                        onOpenPerson(name)
                    } label: {
                        HStack(spacing: 5) {
                            Text("\(name)\u{2019}s days").font(.cavnarBody(14, weight: 700))
                            Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold))
                        }
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: 36)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarAmber.opacity(0.08)))
    }
}

// MARK: - Closing (F1-12, D-43 / D-9)

/// One "stays until close + N min" per role, any role whose two old close
/// settings disagree, the closers per role with a way into the cleanup,
/// and the trading days with no close time — on which nothing about
/// closing can be checked.
struct RulesClosingSection: View {
    @Bindable var store: TeamSetupStore
    let roles: [String]
    @Binding var stays: [String: String]
    @Binding var edited: Bool
    var focus: FocusState<String?>.Binding
    let onOpenClosers: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Closing")
            Text("Minutes past close each role stays \u{2014} the one setting the draft keeps them to and is never cut short of. Blank means they leave at close.")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if !store.closeTimesMissing.isEmpty {
                CavnarCaveat(
                    title: "No close time on " + store.closeTimesMissing.joined(separator: ", "),
                    detail: "Who\u{2019}s on at close, the closers and these minutes can\u{2019}t be checked on those days. "
                        + "Set close times in Account \u{2192} Profile \u{2192} Hours & closures.")
            }
            ForEach(store.roleCloseConflicts) { c in
                HomeMixedText.make(c.sentence, size: 13.5, weight: 600, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if roles.isEmpty {
                Text("Roles appear here once there is shift history to read them from.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .italic()
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(roles.enumerated()), id: \.element) { index, role in
                        HStack(spacing: 10) {
                            Text(role)
                                .font(.cavnarBody(15, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Spacer(minLength: 6)
                            TextField("0", text: Binding(
                                get: { stays[role] ?? "" },
                                set: { stays[role] = $0; edited = true }))
                                .font(.cavnarNumber(15, weight: 700))
                                .foregroundStyle(Color.cavnarInk)
                                .multilineTextAlignment(.center)
                                .keyboardType(.numberPad)
                                .focused(focus, equals: "close|\(role)")
                                .frame(width: 52, height: 32)
                                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper2))
                                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                                    .strokeBorder(focus.wrappedValue == "close|\(role)" ? Color.cavnarEmber : Color.cavnarPaper3,
                                                  lineWidth: 1))
                                .accessibilityLabel("\(role) stays this many minutes past close")
                            Text("MIN")
                                .font(.cavnarBody(11, weight: 700))
                                .tracking(0.5)
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        .padding(.vertical, 9)
                        if index < roles.count - 1 { AccountRowDivider() }
                    }
                }
                .accountCard()
            }
            closersLink
        }
    }

    private var closersLink: some View {
        Button {
            Haptic.light()
            onOpenClosers()
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "lock.fill")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber)
                    .padding(.top, 3)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Closers")
                        .font(.cavnarBody(14.5, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    if let summary = store.closerSummary {
                        ForEach(summary.byRole) { group in
                            HomeMixedText.make("\(group.role): " + group.closers.prefix(4).joined(separator: ", ")
                                               + (group.closers.count > 4 ? " +\(group.closers.count - 4)" : ""),
                                               size: 13, color: .cavnarInk3)
                                .lineLimit(1)
                        }
                        if let warning = summary.warning {
                            HomeMixedText.make(warning.components(separatedBy: ". ").first.map { $0 + "." } ?? warning,
                                               size: 13, weight: 600, color: .cavnarAmber)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    } else {
                        Text("Who closes for each role").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .padding(.top, 3)
            }
            .padding(12)
            .contentShape(Rectangle())
            .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .fill(Color.cavnarPaper2.opacity(0.5)))
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Salaried (F1 E-12, A2-8, F2-3 D-7)

/// The salaried weekly cap — the most hours a salaried person is given
/// with no maximum of their own, 55 unless the owner sets it — each
/// salaried person it applies to, and any salaried name that matches
/// nobody on the roster, with the roster name to link it to.
struct RulesSalariedSection: View {
    @Bindable var store: TeamSetupStore
    @Binding var cap: String
    @Binding var edited: Bool
    var focus: FocusState<String?>.Binding
    let canEdit: Bool
    /// Each person's own maximum, by name, from the roster.
    let ownMax: [String: Double]

    private var capHours: Double {
        if let v = Double(cap.trimmingCharacters(in: .whitespaces)), v > 0 { return v }
        return store.salariedCap ?? store.salariedCapDefault
    }

    private var people: [String] {
        var out: [String] = []
        for m in store.managers?.salaried ?? [] where !out.contains(m.name) { out.append(m.name) }
        for s in store.salaried {
            let n = s.matched ?? s.name
            if !out.contains(n) { out.append(n) }
        }
        return out
    }

    var body: some View {
        AccountSection(kicker: "Salaried") {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .center, spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Salaried weekly cap").font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                        HomeMixedText.make("default \(Self.hours(store.salariedCapDefault)) hours \u{00B7} "
                                           + "\(Self.hours(store.salariedCapBounds.lowerBound))\u{2013}\(Self.hours(store.salariedCapBounds.upperBound))",
                                           size: 13, color: .cavnarInk3.opacity(0.8))
                    }
                    Spacer(minLength: 8)
                    TextField(Self.hours(store.salariedCapDefault), text: Binding(
                        get: { cap }, set: { cap = $0; edited = true }))
                        .font(.cavnarNumber(15, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                        .multilineTextAlignment(.center)
                        .keyboardType(.decimalPad)
                        .focused(focus, equals: "salaried_cap")
                        .frame(width: 66, height: 34)
                        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper2))
                        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .strokeBorder(focus.wrappedValue == "salaried_cap" ? Color.cavnarEmber : Color.cavnarPaper3,
                                          lineWidth: 1))
                        .disabled(!canEdit)
                        .accessibilityLabel("Salaried weekly cap, hours")
                }
                .frame(minHeight: AccountKVRow<EmptyView>.rowHeight)
                .padding(.vertical, 9)
                SetupHelp(text: "Salaried people owe no overtime, and their hours aren\u{2019}t spent from the hourly budget. "
                          + "The draft gives each at most this many hours a week unless their own maximum says otherwise.")
                    .padding(.bottom, 9)
                // The server's own list when it sends one (the cap code
                // holds each to); else the salaried managers and the
                // salaried list, against the cap.
                if !store.salariedCaps.isEmpty {
                    ForEach(store.salariedCaps) { s in
                        AccountRowDivider()
                        AccountKVRow(label: s.name, showsDivider: false) {
                            AccountValue(text: s.own ? "\(Self.hours(s.cap ?? capHours))h, their own"
                                            : "\(Self.hours(edited ? capHours : (s.cap ?? capHours)))h cap",
                                         isNumber: true, tone: .cavnarInk2)
                        }
                    }
                } else {
                    ForEach(people, id: \.self) { name in
                        AccountRowDivider()
                        AccountKVRow(label: name, showsDivider: false) {
                            AccountValue(text: ownMax[name].map { "\(Self.hours($0))h, their own" }
                                            ?? "\(Self.hours(capHours))h cap", isNumber: true, tone: .cavnarInk2)
                        }
                    }
                }
                ForEach(store.salaried.filter { $0.warning != nil }) { entry in
                    AccountRowDivider()
                    VStack(alignment: .leading, spacing: 8) {
                        HomeMixedText.make(entry.warning ?? "", size: 13.5, weight: 600, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                        if let suggestion = entry.suggestion, canEdit {
                            Button {
                                Task { await store.linkSalaried(entry, to: suggestion) }
                            } label: {
                                Group {
                                    if store.salariedBusy == entry.name { CavnarShimmerText(text: "Linking\u{2026}") }
                                    else { Text("Link to \(suggestion)") }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: store.salariedBusy != nil))
                            .disabled(store.salariedBusy != nil)
                        }
                    }
                    .padding(.vertical, 10)
                }
                if let error = store.salariedError {
                    Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 9)
                }
            }
        }
    }

    static func hours(_ v: Double) -> String {
        v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v)
    }
}

// MARK: - Staffing rules as checked (F1 D-14, F2 D-38)

/// Each staffing rule the owner wrote, read back the way the schedule
/// checks it, and the ones it can't check — for the owner to check the
/// week against themselves.
struct RulesOwnerRulesSection: View {
    let rules: [OwnerRuleReadback]
    let unchecked: [String]

    var body: some View {
        if !rules.isEmpty || !unchecked.isEmpty {
            AccountSection(kicker: "Your staffing rules") {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(rules) { rule in
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(Color.cavnarGreen)
                                .padding(.top, 3)
                            VStack(alignment: .leading, spacing: 2) {
                                if let text = rule.text {
                                    HomeMixedText.make(text, size: 14, color: .cavnarInk2)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                if let reads = rule.readsAs {
                                    HomeMixedText.make("Checked on every draft as: \(reads).", size: 13, color: .cavnarInk3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                    }
                    ForEach(unchecked, id: \.self) { text in
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "exclamationmark.circle")
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(Color.cavnarAmber)
                                .padding(.top, 3)
                            VStack(alignment: .leading, spacing: 2) {
                                HomeMixedText.make(text, size: 14, color: .cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                                Text("Not checked by the schedule \u{2014} a draft is asked to follow it; check the week yourself.")
                                    .font(.cavnarBody(13))
                                    .foregroundStyle(Color.cavnarAmber)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                .padding(.vertical, 10)
            }
        }
    }
}
