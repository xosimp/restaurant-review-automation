import SwiftUI

/// The compliance rules the generator will not break, and the headcount
/// floors per role. Every field shows its default as the placeholder;
/// a blank field means "use the default".
struct ScheduleRulesSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    @Environment(\.dismiss) private var dismiss

    @State private var drafts: [String: String] = [:]
    @State private var floors: [String: RoleFloor] = [:]
    @State private var expandedRole: String?
    @State private var posted: String?
    @State private var synced = false
    @FocusState private var focused: String?

    /// Every rule, in the order an owner would read them, with the label
    /// and the unit the number is in. Mirrors the keys the API names.
    private static let fields: [(key: String, label: String, hint: String)] = [
        ("min_rest_hours", "Rest between shifts", "hours"),
        ("max_shift_hours", "Longest shift", "hours"),
        ("daily_ot_hours", "Daily overtime after", "hours"),
        ("meal_break_after_hours", "Meal break after", "hours"),
        ("weekly_hours_ceiling", "Hours a week, at most", "hours"),
        ("min_consecutive_days_off", "Consecutive days off, at least", "days"),
        ("part_time_days_off", "Part-time days off a week", "days"),
        ("notice_days", "Notice before the week starts", "days"),
        ("minor_latest_end", "Minors finish by", "time"),
        ("minor_max_daily_hours", "Minors' longest day", "hours"),
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Hard rules the generator will not break, and what it flags for you. Leave a field blank to keep the default shown.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    if viewModel.isLoadingRules && viewModel.rules.isEmpty && viewModel.ruleDefaults.isEmpty {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.9, 0.6])
                    } else {
                        rulesSection
                        floorsSection
                    }

                    if let error = viewModel.rulesError {
                        Text(error)
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Button {
                        focused = nil
                        Haptic.medium()
                        Task {
                            if await viewModel.saveRules(parsedRules(), roleFloors: cleanedFloors()) {
                                posted = "Rules saved"
                            }
                        }
                    } label: {
                        Group {
                            if viewModel.isSavingRules {
                                CavnarShimmerText(text: "Saving…")
                            } else {
                                Text("Save rules")
                            }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSavingRules))
                    .disabled(viewModel.isSavingRules)
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
            .accountSheetChrome("Schedule rules")
            .cavnarPostedOverlay(posted) {
                posted = nil
                dismiss()
            }
        }
        .task {
            await viewModel.loadRules()
            sync()
        }
        .onChange(of: viewModel.rules) { _, _ in sync() }
    }

    // MARK: Rules

    private var rulesSection: some View {
        AccountSection(kicker: "Rules") {
            ForEach(Array(Self.fields.enumerated()), id: \.element.key) { index, field in
                ruleRow(field.key, label: field.label, hint: field.hint,
                        showsDivider: index < Self.fields.count - 1)
            }
        }
    }

    private func ruleRow(_ key: String, label: String, hint: String, showsDivider: Bool) -> some View {
        let placeholder = viewModel.ruleDefaults[key]?.display ?? "—"
        let isTime = hint == "time"
        return VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(label).font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                    HomeMixedText.make("default \(placeholder)\(isTime ? "" : " \(hint)")", size: 13, color: .cavnarInk3.opacity(0.8))
                }
                Spacer(minLength: 8)
                TextField(placeholder, text: binding(for: key))
                    .font(isTime ? .cavnarBody(15, weight: 700) : .cavnarNumber(15, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .multilineTextAlignment(.center)
                    .keyboardType(isTime ? .default : .decimalPad)
                    .autocorrectionDisabled()
                    .focused($focused, equals: key)
                    .frame(width: isTime ? 84 : 66, height: 34)
                    .background(
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .fill(Color.cavnarPaper2))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .strokeBorder(focused == key ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
            }
            .frame(minHeight: AccountKVRow<EmptyView>.rowHeight)
            .padding(.vertical, 9)
            if showsDivider { AccountRowDivider() }
        }
    }

    private func binding(for key: String) -> Binding<String> {
        Binding(get: { drafts[key] ?? "" }, set: { drafts[key] = $0 })
    }

    // MARK: Role floors

    private var roles: [String] {
        var out = viewModel.ruleRoles
        for role in floors.keys where !out.contains(role) { out.append(role) }
        return out
    }

    private var floorsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Role floors")
            Text("At least this many in a role on a morning and on a night. Open a role for a different number on one day.")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if roles.isEmpty {
                Text("Roles appear here once there is shift history to read them from.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .italic()
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(roles.enumerated()), id: \.element) { index, role in
                        floorRow(role)
                        if index < roles.count - 1 { AccountRowDivider() }
                    }
                }
                .accountCard()
            }
        }
        .animation(.easeOut(duration: 0.22), value: expandedRole)
    }

    private func floorRow(_ role: String) -> some View {
        let open = expandedRole == role
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Button {
                    Haptic.selection()
                    expandedRole = open ? nil : role
                } label: {
                    HStack(spacing: 6) {
                        Text(role)
                            .font(.cavnarBody(15, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        Image(systemName: "chevron.down")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(open ? Color.cavnarEmber : Color.cavnarInk3)
                            .rotationEffect(.degrees(open ? 180 : 0))
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                Spacer(minLength: 6)
                floorField(label: "AM", value: floorBinding(role, day: nil, morning: true), id: "\(role)|am")
                floorField(label: "PM", value: floorBinding(role, day: nil, morning: false), id: "\(role)|pm")
            }
            .padding(.vertical, 9)
            if open {
                VStack(spacing: 6) {
                    ForEach(LaborDayOfWeek.allNames, id: \.self) { day in
                        HStack(spacing: 10) {
                            Text(String(day.prefix(3)))
                                .font(.cavnarBody(13.5, weight: 600))
                                .foregroundStyle(Color.cavnarInk2)
                                .frame(width: 34, alignment: .leading)
                            Spacer(minLength: 6)
                            floorField(label: "AM", value: floorBinding(role, day: day, morning: true), id: "\(role)|\(day)|am")
                            floorField(label: "PM", value: floorBinding(role, day: day, morning: false), id: "\(role)|\(day)|pm")
                        }
                    }
                    Text("Blank uses the role's own AM/PM number.")
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.leading, 12)
                .padding(.bottom, 10)
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.cavnarEmber.opacity(0.4)).frame(width: 2)
                }
                .transition(.opacity)
            }
        }
    }

    private func floorField(label: String, value: Binding<String>, id: String) -> some View {
        HStack(spacing: 4) {
            Text(label)
                .font(.cavnarBody(11, weight: 700))
                .tracking(0.5)
                .foregroundStyle(Color.cavnarInk3)
            TextField("—", text: value)
                .font(.cavnarNumber(15, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .multilineTextAlignment(.center)
                .keyboardType(.numberPad)
                .focused($focused, equals: id)
                .frame(width: 44, height: 32)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(focused == id ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
        }
    }

    private func floorBinding(_ role: String, day: String?, morning: Bool) -> Binding<String> {
        Binding(
            get: {
                let floor = floors[role]
                let value: Int? = day.map { d in
                    morning ? floor?.days?[d]?.morning : floor?.days?[d]?.night
                } ?? (morning ? floor?.morning : floor?.night)
                return value.map(String.init) ?? ""
            },
            set: { text in
                let value = Int(text.trimmingCharacters(in: .whitespaces))
                var floor = floors[role] ?? RoleFloor()
                if let day {
                    var days = floor.days ?? [:]
                    var one = days[day] ?? DayFloor()
                    if morning { one.morning = value } else { one.night = value }
                    days[day] = one
                    floor.days = days
                } else if morning {
                    floor.morning = value
                } else {
                    floor.night = value
                }
                floors[role] = floor
            })
    }

    // MARK: Sync & parse

    private func sync() {
        guard !synced || drafts.isEmpty else { return }
        synced = true
        var next: [String: String] = [:]
        for field in Self.fields {
            if let v = viewModel.rules[field.key]?.display { next[field.key] = v }
        }
        drafts = next
        floors = viewModel.roleFloors
    }

    /// A number where one was typed, the text otherwise (the minors'
    /// finish time), and nothing at all for a blank — blank means default.
    private func parsedRules() -> [String: LooseValue] {
        var out: [String: LooseValue] = [:]
        for (key, text) in drafts {
            let trimmed = text.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty else { continue }
            if let n = NumberFormatter.cavnarDecimal.number(from: trimmed)?.doubleValue ?? Double(trimmed) {
                out[key] = .number(n)
            } else {
                out[key] = .string(trimmed)
            }
        }
        return out
    }

    /// Drop empty day overrides and roles with nothing set, so the server
    /// stores floors rather than a shape full of nulls.
    private func cleanedFloors() -> [String: RoleFloor] {
        var out: [String: RoleFloor] = [:]
        for (role, floor) in floors {
            var cleaned = floor
            let days = (floor.days ?? [:]).filter { $0.value.morning != nil || $0.value.night != nil }
            cleaned.days = days.isEmpty ? nil : days
            if cleaned.morning != nil || cleaned.night != nil || cleaned.days != nil {
                out[role] = cleaned
            }
        }
        return out
    }
}
