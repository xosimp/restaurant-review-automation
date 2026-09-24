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
    // The second batch's settings, all saved with the one Save button.
    @State private var jurisdiction: String = ""
    @State private var managerOnDuty = false
    // A keyholder on until close every open day — on by default (NS5 M8).
    @State private var keyholderUntilClose = true
    @State private var arrivals: [String: String] = [:]
    @State private var requirements: [String: [String]] = [:]
    @State private var fohRoles: [String] = []
    @State private var patioRoles: [String] = []
    @State private var crossTraining: [String: String] = [:]
    @State private var trimToBudget = true
    @State private var cutFloor = 2
    @State private var reservationProvider: String = ""
    @State private var reservationKey: String = ""

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
        ("max_consecutive_days", "Days in a row, at most", "days"),
        ("notice_days", "Notice before the week starts", "days — a week inside it is held"),
        ("minor_latest_end", "Minors finish by", "time"),
        ("minor_max_daily_hours", "Minors' longest day", "hours"),
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Hard rules the generator will not break, and what it flags for you. Leave a field blank to keep the default shown. Everything here saves with the one button at the bottom.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    if viewModel.isLoadingRules && viewModel.rules.isEmpty && viewModel.ruleDefaults.isEmpty {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.9, 0.6])
                    } else {
                        jurisdictionSection
                        rulesSection
                        managerSection
                        floorsSection
                        cutFloorSection
                        arrivalsSection
                        crossTrainingSection
                        certificationsSection
                        roleListSection("Front of house", detail: "Roles counted against sections and the section cap.",
                                        selection: $fohRoles)
                        roleListSection("Patio", detail: "Roles the weather read can thin or thicken.",
                                        selection: $patioRoles)
                        budgetSection
                        reservationSection
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
                            if await viewModel.saveRules(patch()) {
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
            syncUnlessEdited()
        }
        // A reload (a save's answer, another device's change) refreshes
        // the fields only while the manager hasn't touched them. It used to
        // re-sync unconditionally whenever nothing had been synced into
        // `drafts` yet, replacing a jurisdiction or floor mid-edit (CLIENT-60).
        .onChange(of: viewModel.rules) { _, _ in syncUnlessEdited() }
    }

    // MARK: Jurisdiction

    /// Where the restaurant is. A pack sets a few rules to the state's
    /// floor; its notes are what to check with counsel, not law.
    private var jurisdictionSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountSection(kicker: "Where you are") {
                AccountKVRow(label: "Jurisdiction", showsDivider: false) {
                    selectMenu(
                        current: viewModel.packs.first { $0.code == jurisdiction }?.label ?? (jurisdiction.isEmpty ? "None" : jurisdiction),
                        options: [("", "None")] + viewModel.packs.map { ($0.code, $0.label) }
                    ) { jurisdiction = $0 }
                }
            }
            if let pack = viewModel.pack, pack.code == jurisdiction, !jurisdiction.isEmpty {
                if let applied = pack.applied, !applied.isEmpty {
                    AccountFlowLayout(spacing: 6) {
                        ForEach(applied.keys.sorted(), id: \.self) { key in
                            AccountChip(text: "\(Self.fields.first { $0.key == key }?.label ?? key.replacingOccurrences(of: "_", with: " ")) \(applied[key]?.display ?? "") — from the \(pack.label ?? "") pack", muted: true)
                        }
                    }
                }
                ForEach(Array((pack.notes ?? []).enumerated()), id: \.offset) { _, note in
                    CavnarCaveat(title: "Check with counsel", detail: note)
                }
            } else if !jurisdiction.isEmpty, viewModel.pack?.code != jurisdiction {
                Text("Save to apply this pack and read its notes.")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    /// A choice as a menu on a KV row — the kit's answer to a select.
    private func selectMenu(current: String, options: [(String, String)], onPick: @escaping (String) -> Void) -> some View {
        Menu {
            ForEach(options, id: \.0) { code, label in
                Button {
                    Haptic.selection()
                    onPick(code)
                } label: { Text(label) }
            }
        } label: {
            HStack(spacing: 6) {
                Text(current)
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .padding(.horizontal, 10)
            .frame(height: 32)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(Color.cavnarPaper2))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        }
    }

    // MARK: Manager on duty

    private var managerSection: some View {
        AccountSection(kicker: "Leadership") {
            AccountSwitchRow(label: "Manager on duty",
                             detail: "Every shift needs a manager or keyholder on it. Flagged as a hard break when nobody is.",
                             isOn: $managerOnDuty, showsDivider: true)
            AccountSwitchRow(label: "Keyholder until close",
                             detail: "Someone who can close stays until close every open day. Checked once anyone is marked a keyholder or closer.",
                             isOn: $keyholderUntilClose, showsDivider: false)
        }
    }

    // MARK: Arrivals

    /// Minutes before (negative) or after open each role should arrive.
    private var arrivalsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Arrivals")
            Text("Minutes before open a role arrives — 30 means half an hour early, -15 a quarter hour after. Blank means at open.")
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
                        HStack(spacing: 10) {
                            Text(role)
                                .font(.cavnarBody(15, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Spacer(minLength: 6)
                            floorField(label: "MIN", value: Binding(
                                get: { arrivals[role] ?? "" },
                                set: { arrivals[role] = $0 }), id: "arr|\(role)", keyboard: .numbersAndPunctuation)
                        }
                        .padding(.vertical, 9)
                        if index < roles.count - 1 { AccountRowDivider() }
                    }
                }
                .accountCard()
            }
        }
    }

    // MARK: Cross-training per role

    /// How much of each role on a shift should be able to cover a second
    /// station. Blank uses the default shown as the placeholder; 0 means
    /// the role is not expected to flex and is not judged on it.
    private var crossTrainingSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Cross-training")
            Text("The share of each role on a shift that should be able to cover a second station. Blank uses the default shown; 0 means not expected.")
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
                        HStack(spacing: 10) {
                            Text(role)
                                .font(.cavnarBody(15, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Spacer(minLength: 6)
                            percentField(role: role)
                        }
                        .padding(.vertical, 9)
                        if index < roles.count - 1 { AccountRowDivider() }
                    }
                }
                .accountCard()
            }
        }
    }

    private func percentField(role: String) -> some View {
        let id = "ct|\(role)"
        let placeholder = String(viewModel.crossTrainingDefaults[role] ?? viewModel.crossTrainingDefault)
        return HStack(spacing: 4) {
            TextField(placeholder, text: Binding(
                get: { crossTraining[role] ?? "" },
                set: { crossTraining[role] = $0 }))
                .font(.cavnarNumber(15, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .multilineTextAlignment(.center)
                .keyboardType(.numberPad)
                .focused($focused, equals: id)
                .frame(width: 52, height: 32)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(focused == id ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
                .accessibilityLabel("\(role) cross-training target, percent")
            Text("%")
                .font(.cavnarNumber(13, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
        }
    }

    // MARK: Certifications per role

    private var certificationsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Certifications a role needs")
            Text("Only somebody holding every one of these is put on the role.")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if roles.isEmpty || viewModel.ruleCertifications.isEmpty {
                Text(roles.isEmpty ? "Roles appear here once there is shift history to read them from."
                                   : "No certifications are defined yet.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .italic()
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(roles.enumerated()), id: \.element) { index, role in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(role)
                                .font(.cavnarBody(15, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            AccountFlowLayout(spacing: 6) {
                                ForEach(viewModel.ruleCertifications, id: \.self) { cert in
                                    let on = (requirements[role] ?? []).contains(cert)
                                    Button {
                                        Haptic.selection()
                                        var next = requirements[role] ?? []
                                        if on { next.removeAll { $0 == cert } } else { next.append(cert) }
                                        requirements[role] = next
                                    } label: { AccountChip(text: cert, muted: !on) }
                                    .buttonStyle(.plain)
                                    .accessibilityAddTraits(on ? .isSelected : [])
                                }
                            }
                        }
                        .padding(.vertical, 9)
                        if index < roles.count - 1 { AccountRowDivider() }
                    }
                }
                .accountCard()
            }
        }
    }

    // MARK: Role lists (front of house, patio)

    private func roleListSection(_ title: String, detail: String, selection: Binding<[String]>) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: title)
            Text(detail)
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            let all = roles + selection.wrappedValue.filter { !roles.contains($0) }
            if all.isEmpty {
                Text("Roles appear here once there is shift history to read them from.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .italic()
            } else {
                AccountFlowLayout(spacing: 6) {
                    ForEach(all, id: \.self) { role in
                        let on = selection.wrappedValue.contains(role)
                        Button {
                            Haptic.selection()
                            var next = selection.wrappedValue
                            if on { next.removeAll { $0 == role } } else { next.append(role) }
                            selection.wrappedValue = next
                        } label: { AccountChip(text: role, muted: !on) }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
            }
        }
    }

    // MARK: Budget

    private var budgetSection: some View {
        AccountSection(kicker: "Budget") {
            AccountSwitchRow(label: "Trim to budget",
                             detail: "Remove the least-needed shifts until the week fits the hours budget. Each trim is listed with its reason.",
                             isOn: $trimToBudget, showsDivider: false)
        }
    }

    // MARK: Reservations

    private var reservationSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountSection(kicker: "Reservation system") {
                AccountKVRow(label: "System") {
                    selectMenu(
                        current: viewModel.reservationProviders.first { $0.code == reservationProvider }?.label
                            ?? (reservationProvider.isEmpty ? "None" : reservationProvider),
                        options: [("", "None")] + viewModel.reservationProviders.map { ($0.code, $0.label) }
                    ) { reservationProvider = $0 }
                }
                HStack(alignment: .center, spacing: 12) {
                    Text("API key").font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                    Spacer(minLength: 8)
                    SecureField(viewModel.reservationFeed?.configured == true ? "on file" : "paste the key", text: $reservationKey)
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk)
                        .multilineTextAlignment(.trailing)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .focused($focused, equals: "reservation_key")
                        .frame(maxWidth: 200)
                }
                .frame(minHeight: AccountKVRow<EmptyView>.rowHeight)
                .padding(.vertical, 9)
            }
            if let feed = viewModel.reservationFeed, let message = feed.message, !message.isEmpty {
                HStack(alignment: .top, spacing: 7) {
                    Circle().fill(feed.live == true ? Color.cavnarGreen : Color.cavnarInk3)
                        .frame(width: 6, height: 6).padding(.top, 6)
                    HomeMixedText.make(message, size: 13, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    Task { await viewModel.syncReservations() }
                } label: {
                    Group {
                        if viewModel.isSyncingReservations {
                            CavnarShimmerText(text: "Syncing…")
                        } else {
                            HStack(spacing: 6) {
                                Image(systemName: "arrow.triangle.2.circlepath").font(.system(size: 11, weight: .bold))
                                Text("Sync now")
                            }
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.isSyncingReservations)
            }
            if let message = viewModel.reservationSyncMessage {
                HomeMixedText.make(message, size: 13.5, weight: 600, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
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
        for role in arrivals.keys where !out.contains(role) { out.append(role) }
        for role in requirements.keys where !out.contains(role) { out.append(role) }
        for role in crossTraining.keys.sorted() where !out.contains(role) { out.append(role) }
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

    // MARK: Cut floor

    /// "Never cut a role below N people" — the floor a send-home
    /// suggestion keeps for any role without one of its own above. Cuts
    /// only: it never adds a shift or flags a week.
    private var cutFloorSection: some View {
        AccountSection(kicker: "Cuts") {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Never cut a role below").font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                    Text("Used for any role without its own floor. Cavnar AI never suggests sending someone home if it would leave fewer than this on.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3.opacity(0.8))
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                HomeMixedText.make("\(cutFloor) \(cutFloor == 1 ? "person" : "people")", size: 15, color: .cavnarInk,
                                   numberWeight: 700)
                    .monospacedDigit()
                    .fixedSize()
                Stepper("Never cut a role below", value: $cutFloor, in: 1...max(1, viewModel.cutFloorMax))
                    .labelsHidden()
                    .fixedSize()
                    .tint(Color.cavnarEmber)
                    .disabled(!viewModel.canEditRules)
                    .accessibilityValue("\(cutFloor) \(cutFloor == 1 ? "person" : "people")")
            }
            .frame(minHeight: AccountKVRow<EmptyView>.rowHeight)
            .padding(.vertical, 9)
        }
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

    private func floorField(label: String, value: Binding<String>, id: String,
                            keyboard: UIKeyboardType = .numberPad) -> some View {
        HStack(spacing: 4) {
            Text(label)
                .font(.cavnarBody(11, weight: 700))
                .tracking(0.5)
                .foregroundStyle(Color.cavnarInk3)
            TextField("—", text: value)
                .font(.cavnarNumber(15, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .multilineTextAlignment(.center)
                .keyboardType(keyboard)
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

    /// What the fields said right after the last sync — so an edit since
    /// can be told apart from a sheet nobody has touched.
    @State private var syncedSignature: Data?

    private var fieldsSignature: Data? {
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys
        return try? encoder.encode(patch())
    }

    private func syncUnlessEdited() {
        if synced, fieldsSignature != syncedSignature { return }
        sync()
    }

    private func sync() {
        synced = true
        defer { syncedSignature = fieldsSignature }
        var next: [String: String] = [:]
        for field in Self.fields {
            if let v = viewModel.rules[field.key]?.display { next[field.key] = v }
        }
        drafts = next
        floors = viewModel.roleFloors
        jurisdiction = viewModel.jurisdiction ?? ""
        if case .bool(let b) = viewModel.rules["manager_on_duty"] ?? .null { managerOnDuty = b }
        else if case .number(let n) = viewModel.rules["manager_on_duty"] ?? .null { managerOnDuty = n != 0 }
        if case .bool(let b) = viewModel.rules["keyholder_until_close"] ?? .null { keyholderUntilClose = b }
        else if case .number(let n) = viewModel.rules["keyholder_until_close"] ?? .null { keyholderUntilClose = n != 0 }
        arrivals = viewModel.roleArrivals.mapValues(String.init)
        requirements = viewModel.roleRequirements
        fohRoles = viewModel.fohRoles
        patioRoles = viewModel.patioRoles
        crossTraining = viewModel.roleCrossTraining.mapValues(String.init)
        trimToBudget = viewModel.trimToBudget
        cutFloor = viewModel.cutFloorDefault
        reservationProvider = viewModel.reservationFeed?.provider ?? ""
        reservationKey = ""
    }

    /// Everything on the sheet, in one body. A setting that matches what
    /// the server already holds is still sent — the server treats each
    /// key as the whole value, and the sheet is its source of truth.
    private func patch() -> ScheduleSetupViewModel.RulesPatch {
        var rules = parsedRules()
        rules["manager_on_duty"] = .bool(managerOnDuty)
        rules["keyholder_until_close"] = .bool(keyholderUntilClose)
        var arrivalMinutes: [String: Int] = [:]
        for (role, text) in arrivals {
            if let n = Int(text.trimmingCharacters(in: .whitespaces)) { arrivalMinutes[role] = n }
        }
        var p = ScheduleSetupViewModel.RulesPatch(rules: rules, roleFloors: cleanedFloors())
        p.jurisdiction = .some(jurisdiction.isEmpty ? nil : jurisdiction)
        p.roleArrivals = arrivalMinutes
        p.roleRequirements = requirements.filter { !$0.value.isEmpty }
        p.fohRoles = fohRoles
        p.patioRoles = patioRoles
        var crossPercents: [String: Int] = [:]
        for (role, text) in crossTraining {
            if let n = Int(text.trimmingCharacters(in: .whitespaces)) { crossPercents[role] = max(0, min(100, n)) }
        }
        p.roleCrossTraining = crossPercents
        p.trimToBudget = trimToBudget
        p.cutFloorDefault = cutFloor
        if reservationProvider != (viewModel.reservationFeed?.provider ?? "") || !reservationKey.isEmpty {
            p.reservationProvider = .some(reservationProvider.isEmpty ? nil : reservationProvider)
            if !reservationKey.isEmpty { p.reservationApiKey = .some(reservationKey) }
        }
        return p
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
