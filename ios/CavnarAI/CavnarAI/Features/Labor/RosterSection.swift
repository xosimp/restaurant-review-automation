import SwiftUI

/// The roster as the generator sees it — everyone with a role, their
/// Operational Score, how reliably they turn up, and whether they are
/// on the roster at all. Tap a person for the settings the engine
/// obeys; below the list, the pairs to keep together or apart.
struct RosterSection: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    var onExpand: (() -> Void)? = nil

    @State private var selected: RosterMember?
    @State private var showingPairEditor = false
    @State private var showingRules = false

    var body: some View {
        CavnarDropdown(
            title: "Roster & rules",
            subtitle: subtitle,
            badge: viewModel.roster.filter { !$0.isActive }.isEmpty ? nil : viewModel.roster.filter { !$0.isActive }.count,
            tone: .neutral,
            isExpanded: $viewModel.rosterExpanded,
            onExpand: { onExpand?(); Task { await viewModel.loadRoster() } }
        ) {
            VStack(alignment: .leading, spacing: 14) {
                Text("Who the generator may schedule, and how. Tap a person to set hours, days and status.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)

                if !viewModel.canEditRoster {
                    Text("Read-only on this login — an owner or manager can change these.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarAmber)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if viewModel.isLoadingRoster && viewModel.roster.isEmpty {
                    CavnarSkeletonLines(widths: [1.0, 0.85, 0.7])
                } else if let error = viewModel.rosterError, viewModel.roster.isEmpty {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                } else if viewModel.roster.isEmpty {
                    Text("The roster fills from your shift history — upload shifts under Account and everyone appears here.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    VStack(spacing: 0) {
                        ForEach(viewModel.roster) { member in
                            memberRow(member)
                            if member.id != viewModel.roster.last?.id {
                                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                            }
                        }
                    }
                }

                rulesLink

                pairsBlock

                if let error = viewModel.pairError {
                    Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .sheet(item: $selected) { member in
            RosterDetailSheet(viewModel: viewModel, name: member.name)
        }
        .sheet(isPresented: $showingPairEditor) {
            PairEditorSheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showingRules) {
            ScheduleRulesSheet(viewModel: viewModel)
        }
    }

    private var subtitle: String {
        let active = viewModel.activeRoster.count
        let total = viewModel.roster.count
        if total == 0 { return "Everyone the generator can schedule" }
        if active == total { return "\(total) on the roster" }
        return "\(active) of \(total) on the roster"
    }

    // MARK: Rows

    private func memberRow(_ member: RosterMember) -> some View {
        Button {
            Haptic.light()
            selected = member
        } label: {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(member.name)
                            .font(.cavnarBody(15, weight: 600))
                            .foregroundStyle(member.isActive ? Color.cavnarInk : Color.cavnarInk3)
                        if let type = member.settings?.employmentType, type == "part" {
                            Text("PT")
                                .font(.cavnarBody(10, weight: 700))
                                .tracking(0.5)
                                .foregroundStyle(Color.cavnarInk3)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(Color.white.opacity(0.06)))
                        }
                        if member.settings?.isMinor == true {
                            Text("MINOR")
                                .font(.cavnarBody(10, weight: 700))
                                .tracking(0.5)
                                .foregroundStyle(Color.cavnarBlue)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(Color.cavnarBlue.opacity(0.14)))
                        }
                    }
                    if member.isActive {
                        HomeMixedText.make(detailLine(member), size: 13.5, color: .cavnarInk3)
                            .lineLimit(1)
                    } else {
                        Text("Not on the roster")
                            .font(.cavnarBody(13.5))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                Spacer(minLength: 8)
                if let score = member.score, member.isActive {
                    Text("\(score)")
                        .font(.cavnarNumber(17, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                        .frame(width: 26, alignment: .trailing)
                        .accessibilityLabel("Operational Score \(score)")
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .padding(.vertical, 10)
            .contentShape(Rectangle())
            .opacity(member.isActive ? 1 : 0.55)
        }
        .buttonStyle(.plain)
    }

    private func detailLine(_ member: RosterMember) -> String {
        var parts: [String] = []
        if let role = member.role, !role.isEmpty { parts.append(role) }
        if let r = member.reliability?.noShowLabel { parts.append(r) }
        if member.canClose == true { parts.append("can close") }
        if let s = member.settings, let lo = s.minHours, let hi = s.maxHours, hi > 0 {
            parts.append("\(Int(lo))–\(Int(hi))h")
        }
        return parts.isEmpty ? "No role on file" : parts.joined(separator: " · ")
    }

    // MARK: Rules link

    private var rulesLink: some View {
        Button {
            Haptic.light()
            showingRules = true
        } label: {
            HStack(spacing: 10) {
                Image(systemName: "checklist")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Schedule rules")
                        .font(.cavnarBody(14.5, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    Text("Rest, shift length, minors, role floors")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .padding(12)
            .contentShape(Rectangle())
            .background(
                RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                    .fill(Color.cavnarPaper2.opacity(0.5)))
        }
        .buttonStyle(.plain)
    }

    // MARK: Pairs

    private var pairsBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Pairs")
                    .font(.cavnarBody(14.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                if viewModel.canEditRoster && viewModel.activeRoster.count >= 2 {
                    Button {
                        Haptic.light()
                        showingPairEditor = true
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "plus").font(.system(size: 11, weight: .bold))
                            Text("Add").font(.cavnarBody(14, weight: 600))
                        }
                        .foregroundStyle(Color.cavnarEmber)
                    }
                    .buttonStyle(.plain)
                }
            }
            Text("Two people to keep on the same shift, or apart.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
            if viewModel.pairs.isEmpty {
                Text("No pairs yet.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .italic()
            } else {
                // A List for the system swipe-to-delete, sized to its rows
                // because it sits inside the page's own ScrollView.
                List {
                    ForEach(viewModel.pairs) { pair in
                        pairRow(pair)
                            .listRowBackground(Color.clear)
                            .listRowInsets(EdgeInsets(top: 6, leading: 0, bottom: 6, trailing: 0))
                            .listRowSeparatorTint(Color.cavnarPaper3.opacity(0.5))
                    }
                    .onDelete { offsets in
                        guard viewModel.canEditRoster else { return }
                        let ids = offsets.map { viewModel.pairs[$0].id }
                        Task { for id in ids { await viewModel.deletePair(id: id) } }
                    }
                    .deleteDisabled(!viewModel.canEditRoster)
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .scrollDisabled(true)
                .frame(height: CGFloat(viewModel.pairs.count) * 56)
            }
        }
    }

    private func pairRow(_ pair: StaffPair) -> some View {
        HStack(spacing: 10) {
            Image(systemName: pair.isPrefer ? "person.2.fill" : "person.2.slash.fill")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(pair.isPrefer ? Color.cavnarGreen : Color.cavnarRed)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(pair.a) \(pair.isPrefer ? "with" : "apart from") \(pair.b)")
                    .font(.cavnarBody(14.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                if let note = pair.note, !note.isEmpty {
                    Text(note).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer()
        }
    }
}

// MARK: - Detail sheet

/// One person's settings, saved a field at a time as they change.
private struct RosterDetailSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    let name: String
    @Environment(\.dismiss) private var dismiss

    @State private var active = true
    @State private var isMinor = false
    @State private var employmentType = "full"
    @State private var minHours = ""
    @State private var maxHours = ""
    @State private var dayparts: [String: String] = [:]
    @State private var toast: String?
    @FocusState private var focused: Field?

    private enum Field: Hashable, CaseIterable { case minHours, maxHours }

    private var member: RosterMember? { viewModel.roster.first { $0.name == name } }
    private var busy: Bool { viewModel.savingFor == name }
    private var editable: Bool { viewModel.canEditRoster }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    hero
                    if !editable {
                        Text("Read-only on this login — an owner or manager can change these.")
                            .font(.cavnarBody(13.5))
                            .foregroundStyle(Color.cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    statusSection
                    hoursSection
                    daypartSection
                    if let toast {
                        Text(toast)
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
            .accountSheetChrome(name)
            .keyboardNavToolbar($focused)
        }
        .onAppear(perform: sync)
        .onChange(of: viewModel.settingsToast) { _, message in
            guard let message else { return }
            toast = message
            sync()
        }
        .onChange(of: focused) { old, new in
            // Hours commit when the field loses focus, so a half-typed
            // "1" on the way to "16" never becomes the ceiling.
            if old == .minHours, new != .minHours { commitHours(min: true) }
            if old == .maxHours, new != .maxHours { commitHours(min: false) }
        }
    }

    private var hero: some View {
        AccountHero(title: name) {
            GlowBadge(systemImage: "person.fill", size: 52)
        } subtitle: {
            HomeMixedText.make(subtitle, size: 15.5, color: .cavnarInk3)
        }
    }

    private var subtitle: String {
        guard let member else { return "" }
        var parts: [String] = []
        if let role = member.role, !role.isEmpty { parts.append(role) }
        if let score = member.score { parts.append("score \(score)") }
        if let r = member.reliability?.noShowLabel { parts.append(r) }
        if let shifts = member.shifts, shifts > 0 { parts.append("\(shifts) shifts") }
        return parts.joined(separator: " · ")
    }

    private var statusSection: some View {
        AccountSection(kicker: "Status") {
            AccountSwitchRow(label: "On the roster",
                             detail: active ? "The generator may schedule them." : "Not on the roster — skipped by every draft.",
                             isOn: Binding(get: { active }, set: { newValue in
                                active = newValue
                                Task { await viewModel.updateSettings(.init(employeeName: name, active: newValue)) }
                             }),
                             busy: busy, disabled: !editable)
            AccountSwitchRow(label: "Minor",
                             detail: "Under 18 — the minor rules (latest end, daily hours) apply.",
                             isOn: Binding(get: { isMinor }, set: { newValue in
                                isMinor = newValue
                                Task { await viewModel.updateSettings(.init(employeeName: name, isMinor: newValue)) }
                             }),
                             busy: busy, disabled: !editable, showsDivider: true)
            AccountKVRow(label: "Employment", showsDivider: false) {
                HStack(spacing: 6) {
                    ForEach(viewModel.employmentTypes, id: \.self) { type in
                        choiceChip(type == "full" ? "Full time" : (type == "part" ? "Part time" : type.capitalized),
                                   on: employmentType == type, enabled: editable) {
                            employmentType = type
                            Task { await viewModel.updateSettings(.init(employeeName: name, employmentType: type)) }
                        }
                    }
                }
            }
        }
    }

    private var hoursSection: some View {
        AccountSection(kicker: "Hours a week") {
            AccountField(label: "At least", text: $minHours, focus: $focused, field: .minHours,
                         keyboardType: .decimalPad, isNumber: true)
                .disabled(!editable)
            AccountField(label: "At most", text: $maxHours, focus: $focused, field: .maxHours,
                         keyboardType: .decimalPad, isNumber: true, showsDivider: false)
                .disabled(!editable)
            Text("Leave blank for no floor or ceiling beyond the week's own limit.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .padding(.top, 8)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var daypartSection: some View {
        AccountSection(kicker: "Which part of each day") {
            ForEach(Array(viewModel.days.enumerated()), id: \.element) { index, day in
                VStack(alignment: .leading, spacing: 8) {
                    Text(day)
                        .font(.cavnarBody(14.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    HStack(spacing: 6) {
                        ForEach(viewModel.dayparts, id: \.self) { part in
                            choiceChip(part.capitalized, on: (dayparts[day] ?? "any") == part,
                                       enabled: editable, tone: part == "off" ? .cavnarRed : .cavnarEmber) {
                                var next = dayparts
                                next[day] = part
                                dayparts = next
                                Task { await viewModel.updateSettings(.init(employeeName: name, daypartAvailability: next)) }
                            }
                        }
                    }
                }
                .padding(.vertical, 9)
                if index < viewModel.days.count - 1 { AccountRowDivider() }
            }
        }
    }

    private func choiceChip(_ label: String, on: Bool, enabled: Bool, tone: Color = .cavnarEmber,
                            action: @escaping () -> Void) -> some View {
        Button {
            guard enabled, !on else { return }
            Haptic.selection()
            action()
        } label: {
            Text(label)
                .font(.cavnarBody(13, weight: on ? 700 : 500))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 32)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? tone : Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
                .opacity(enabled ? 1 : 0.6)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
    }

    private func sync() {
        guard let member else { return }
        let s = member.settings
        active = member.isActive
        isMinor = s?.isMinor ?? false
        employmentType = s?.employmentType ?? "full"
        minHours = s?.minHours.map(Self.hours) ?? ""
        maxHours = s?.maxHours.map(Self.hours) ?? ""
        var parts: [String: String] = [:]
        for day in viewModel.days { parts[day] = s?.daypartAvailability?[day] ?? "any" }
        dayparts = parts
    }

    private static func hours(_ v: Double) -> String {
        v == 0 ? "" : (v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v))
    }

    private func commitHours(min: Bool) {
        let text = (min ? minHours : maxHours).trimmingCharacters(in: .whitespaces)
        let value = text.isEmpty ? 0 : (NumberFormatter.cavnarDecimal.number(from: text)?.doubleValue ?? Double(text))
        guard let value, value >= 0 else {
            toast = "Hours need to be a number."
            sync()
            return
        }
        let current = min ? member?.settings?.minHours : member?.settings?.maxHours
        guard (current ?? 0) != value else { return }
        Task {
            await viewModel.updateSettings(min ? .init(employeeName: name, minHours: value)
                                               : .init(employeeName: name, maxHours: value))
        }
    }
}

// MARK: - Pair editor

private struct PairEditorSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    @Environment(\.dismiss) private var dismiss

    @State private var a = ""
    @State private var b = ""
    @State private var kind = "prefer"
    @State private var note = ""
    @FocusState private var focused: Field?
    private enum Field: Hashable, CaseIterable { case note }

    private var canSave: Bool { !a.isEmpty && !b.isEmpty && a != b && !viewModel.isSavingPair }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    picker("First person", selection: $a, excluding: b)
                    picker("Second person", selection: $b, excluding: a)
                    VStack(alignment: .leading, spacing: 8) {
                        kicker("Keep them")
                        HStack(spacing: 8) {
                            kindChip("Together", value: "prefer", tone: .cavnarGreen)
                            kindChip("Apart", value: "avoid", tone: .cavnarRed)
                        }
                    }
                    CavnarFloatingField(icon: "text.alignleft", placeholder: "Note (optional)", text: $note,
                                        focus: $focused, field: .note)
                    if let error = viewModel.pairError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.addPair(a: a, b: b, kind: kind, note: note) { dismiss() }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingPair {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Save pair")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSave))
                        .disabled(!canSave)
                        Button { dismiss() } label: { Text("Cancel").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("New pair")
            .keyboardNavToolbar($focused)
        }
    }

    private func kicker(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(11.5, weight: 700))
            .tracking(0.8)
            .foregroundStyle(Color.cavnarInk3)
    }

    private func picker(_ label: String, selection: Binding<String>, excluding: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            kicker(label)
            AccountFlowLayout(spacing: 6) {
                ForEach(viewModel.activeNames.filter { $0 != excluding }, id: \.self) { person in
                    Button {
                        Haptic.selection()
                        selection.wrappedValue = person
                    } label: {
                        AccountChip(text: person, muted: selection.wrappedValue != person)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func kindChip(_ label: String, value: String, tone: Color) -> some View {
        let on = kind == value
        return Button {
            Haptic.selection()
            kind = value
        } label: {
            Text(label)
                .font(.cavnarBody(14, weight: on ? 700 : 500))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 36)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? tone : Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
        }
        .buttonStyle(.plain)
    }
}
