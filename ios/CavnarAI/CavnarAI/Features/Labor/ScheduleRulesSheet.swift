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
    // Schedule audit 10/3/26: one "stays until close + N" per role (D-43)
    // and the salaried cap (E-12) — each sent only once the owner touched
    // it, so an unrelated save never settles a close conflict for them.
    @State private var stays: [String: String] = [:]
    @State private var staysEdited = false
    @State private var salariedCap = ""
    @State private var capEdited = false
    @State private var showingClosers = false
    @State private var showingKitchen = false
    @State private var showingSectionNames = false
    @State private var standingFor: StandingPerson?
    /// What "Suggest from history" filled in, said until the next save.
    @State private var floorNote: String?

    private struct StandingPerson: Identifiable { let name: String; var id: String { name } }

    /// Every rule, in the order an owner would read them, with the label
    /// and the unit the number is in. Mirrors the keys the API names.
    private static let fields: [(key: String, label: String, hint: String)] = [
        ("min_rest_hours", "Rest between shifts", "hours"),
        ("max_shift_hours", "Longest shift", "hours"),
        // The shortest shift the owner wants; blank is no rule (schedule
        // audit 10/3/26 E-16).
        ("min_shift_hours", "Shortest shift", "hours"),
        ("daily_ot_hours", "Daily overtime after", "hours"),
        ("meal_break_after_hours", "Meal break after", "hours"),
        ("weekly_hours_ceiling", "Hours a week, at most", "hours"),
        // What "full-time" means for someone with no minimum of their own
        // (schedule audit 10/3/26 D-41); 0 is no minimum.
        ("full_time_min_hours", "Full-time means at least", "hours a week"),
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

                    if !viewModel.rulesLoaded {
                        // Never a form of blanks: Save over it once wiped
                        // every rule the owner had set (schedule re-audit
                        // 10/4/26 UI-2). Loading, or why it could not and
                        // a way to try again.
                        if viewModel.isLoadingRules || viewModel.rulesError == nil {
                            CavnarSkeletonLines(widths: [1.0, 0.8, 0.9, 0.6])
                        } else {
                            ScheduleNotice(text: "Your rules couldn\u{2019}t be loaded, so nothing here can be saved yet.",
                                           tone: .cavnarRed) {
                                Button {
                                    Haptic.light()
                                    Task {
                                        await viewModel.loadRules()
                                        syncUnlessEdited()
                                    }
                                } label: { Text("Try again").frame(maxWidth: .infinity) }
                                    .buttonStyle(CavnarSecondaryButtonStyle())
                            }
                        }
                    } else {
                        jurisdictionSection
                        rulesSection
                        managerSection
                        // Who counts as the manager on the floor, and the
                        // managers whose days are nowhere on file (F1-1).
                        RulesManagersSection(store: viewModel.teamSetup, canEdit: viewModel.canEditRules,
                                             onChanged: { Task { await viewModel.loadRoster() } },
                                             onOpenPerson: { standingFor = StandingPerson(name: $0) })
                        RulesClosingSection(store: viewModel.teamSetup, roles: roles, stays: $stays,
                                            edited: $staysEdited, focus: $focused,
                                            onOpenClosers: { showingClosers = true })
                        if viewModel.canEditRules {
                            RulesSalariedSection(store: viewModel.teamSetup, cap: $salariedCap, edited: $capEdited,
                                                 focus: $focused, canEdit: viewModel.canEditRules,
                                                 ownMax: ownMaxHours)
                            // The rule's words are the owner's — a private
                            // one never reaches anyone else (D-38).
                            RulesOwnerRulesSection(rules: viewModel.teamSetup.ownerRules,
                                                   unchecked: viewModel.teamSetup.ownerRulesUnchecked)
                        }
                        // The hours & shift rules as every draft is checked
                        // against them (PROMPT-1) — the restaurant's own
                        // settings, read by everyone who sees the rules.
                        RulesOwnerRulesSection(rules: viewModel.teamSetup.hoursRules,
                                               unchecked: viewModel.teamSetup.hoursRulesUnchecked,
                                               kicker: "Your hours & shift rules")
                        // The weekdays the restaurant is closed, the
                        // section count and the kitchen's stations save as
                        // they change, apart from Save rules (iOS parity,
                        // 10/7/26 — the web saves each on its own too).
                        RulesClosedDaysSection(store: viewModel.teamSetup, canEdit: viewModel.canEditRules)
                        floorsSection
                        cutFloorSection
                        arrivalsSection
                        crossTrainingSection
                        certificationsSection
                        RulesKitchenSection(store: viewModel.teamSetup) { showingKitchen = true }
                        roleListSection("Front of house", detail: "Roles counted against sections and the section cap.",
                                        selection: $fohRoles)
                        RulesDiningSectionsSection(store: viewModel.teamSetup, canEdit: viewModel.canEditRules) {
                            showingSectionNames = true
                        }
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

                    if viewModel.rulesLoaded {
                        Button {
                            focused = nil
                            Haptic.medium()
                            let changes = patch()
                            if Self.isEmpty(changes) {
                                posted = "Nothing changed"
                                return
                            }
                            Task {
                                if await viewModel.saveRules(changes) {
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
            if viewModel.rulesLoaded { syncUnlessEdited() }
            // The salaried list is the owner's alone; another login is sent none.
            if viewModel.canEditRules { await viewModel.teamSetup.loadSalaried() }
            if viewModel.roster.isEmpty { await viewModel.loadRoster() }
        }
        // A reload (a save's answer, another device's change) refreshes
        // the fields only while the manager hasn't touched them. It used to
        // re-sync unconditionally whenever nothing had been synced into
        // `drafts` yet, replacing a jurisdiction or floor mid-edit (CLIENT-60).
        .onChange(of: viewModel.rules) { _, _ in if viewModel.rulesLoaded { syncUnlessEdited() } }
        .sheet(isPresented: $showingClosers, onDismiss: {
            Task { await viewModel.loadRules(); await viewModel.loadRoster() }
        }) {
            CloserCleanupSheet(viewModel: viewModel)
        }
        .sheet(item: $standingFor, onDismiss: { Task { await viewModel.loadRules() } }) { person in
            RosterDetailSheet(viewModel: viewModel, name: person.name)
        }
        .sheet(isPresented: $showingKitchen) {
            KitchenStationsSheet(store: viewModel.teamSetup, canEdit: viewModel.canEditRules)
                .presentationDetents([.large])
        }
        .sheet(isPresented: $showingSectionNames) {
            FloorSectionsSheet()
                .presentationDetents([.medium, .large])
        }
    }

    /// Each person's own weekly maximum, where the owner set one — the
    /// salaried list says it instead of the cap.
    private var ownMaxHours: [String: Double] {
        var out: [String: Double] = [:]
        for m in viewModel.roster {
            if let v = m.settings?.maxHours, v > 0 { out[m.name] = v }
        }
        return out
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
            // Not a setting (owner, 10/2/26): every minute anybody is on, a
            // manager or owner is too; the server enforces it.
            AccountKVRow(label: "A manager on the floor") {
                Text("Always").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
            }
            AccountSwitchRow(label: "A closer until close",
                             detail: "Somebody marked to close stays until close every open day, the last of their role to leave.",
                             isOn: $keyholderUntilClose, showsDivider: false)
        }
    }

    // MARK: Arrivals

    /// Minutes before their OWN shift start each role clocks in
    /// (attendance.clock_in_leads on the server) — never "before open", and
    /// it never moves a shift. Salaried-only roles don't clock in.
    private var arrivalsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Arrivals")
            Text("How many minutes before their own shift starts each role clocks in. More than 10 minutes past that counts as late. Blank means at the start.")
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
                            if viewModel.rolesWithoutClockIn.contains(where: { $0.caseInsensitiveCompare(role) == .orderedSame }) {
                                Text("Salaried, doesn't clock in")
                                    .font(.cavnarBody(13.5))
                                    .foregroundStyle(Color.cavnarInk3)
                            } else {
                                floorField(label: "MIN", value: Binding(
                                    get: { arrivals[role] ?? "" },
                                    set: { arrivals[role] = $0 }), id: "arr|\(role)", keyboard: .numberPad)
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

    /// The line under a rule: its default, or for the two that have none
    /// worth stating, what blank and zero mean.
    private func subline(_ key: String, placeholder: String, hint: String, isTime: Bool) -> String {
        switch key {
        case "min_shift_hours": return "1\u{2013}12 hours \u{00B7} blank: no shortest shift"
        case "full_time_min_hours": return "default \(placeholder) \(hint) \u{00B7} 0: no minimum"
        default: return "default \(placeholder)\(isTime ? "" : " \(hint)")"
        }
    }

    private func ruleRow(_ key: String, label: String, hint: String, showsDivider: Bool) -> some View {
        let placeholder = viewModel.ruleDefaults[key]?.display ?? "—"
        let isTime = hint == "time"
        return VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(label).font(.cavnarBody(16)).foregroundStyle(Color.cavnarInk3)
                    HomeMixedText.make(subline(key, placeholder: placeholder, hint: hint, isTime: isTime),
                                       size: 13, color: .cavnarInk3.opacity(0.8))
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
        // The roster's roles, so a restaurant that set nothing yet still
        // has a row per role to set.
        for role in viewModel.teamSetup.rosterRoles where !out.contains(role) { out.append(role) }
        for role in floors.keys where !out.contains(role) { out.append(role) }
        for role in stays.keys.sorted() where !out.contains(role) { out.append(role) }
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
            // Floors from the last eight weeks, filled into the empty
            // boxes for the owner to check — saved only with Save rules
            // (schedule audit 10/3/26 D-42).
            if viewModel.canEditRules {
                Button {
                    Task { await suggestFloors() }
                } label: {
                    Group {
                        if viewModel.teamSetup.isSuggestingFloors {
                            CavnarShimmerText(text: "Reading your history\u{2026}")
                        } else {
                            HStack(spacing: 6) {
                                Image(systemName: "wand.and.stars").font(.system(size: 12, weight: .semibold))
                                Text("Suggest from history")
                            }
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: viewModel.teamSetup.isSuggestingFloors))
                .disabled(viewModel.teamSetup.isSuggestingFloors)
            }
            if let note = floorNote {
                HomeMixedText.make(note, size: 13.5, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = viewModel.teamSetup.floorSuggestError {
                Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Floors that ask for more servers than there are sections —
            // no schedule can hold both (schedule audit 10/3/26 P-29).
            ForEach(viewModel.teamSetup.floorCapConflicts) { conflict in
                HStack(alignment: .top, spacing: 7) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.cavnarAmber)
                        .padding(.top, 2)
                    HomeMixedText.make(conflict.sentence, size: 13.5, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
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

    /// "Suggest from history": each box the owner left empty takes the
    /// history's figure; a floor already set is never replaced. Nothing is
    /// saved until Save rules.
    private func suggestFloors() async {
        floorNote = nil
        guard let s = await viewModel.teamSetup.suggestFloors() else { return }
        var next = floors
        var filled = 0
        for (role, spec) in s.floors {
            let key = next.keys.first { $0.caseInsensitiveCompare(role) == .orderedSame } ?? role
            var f = next[key] ?? RoleFloor()
            if (f.morning ?? 0) == 0, let v = spec.morning, v > 0 { f.morning = v; filled += 1 }
            if (f.night ?? 0) == 0, let v = spec.night, v > 0 { f.night = v; filled += 1 }
            for (day, d) in spec.days ?? [:] {
                var days = f.days ?? [:]
                var one = days[day] ?? DayFloor()
                if one.morning == nil, let v = d.morning { one.morning = v; filled += 1 }
                if one.night == nil, let v = d.night { one.night = v; filled += 1 }
                days[day] = one
                f.days = days
            }
            next[key] = f
        }
        floors = next
        floorNote = filled == 0
            ? "Your floors already cover what your history suggests \u{2014} nothing was filled in."
            : "Filled \(filled) \(filled == 1 ? "box" : "boxes") from your history. "
                + (s.note ?? "Check each before you save it.") + " Not saved until you tap Save rules."
    }

    // MARK: Sync & parse

    /// What the fields said right after the last sync — so an edit since
    /// can be told apart from a sheet nobody has touched.
    @State private var syncedSignature: Data?

    private var fieldsSignature: Data? {
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys
        return try? encoder.encode(fullPatch())
    }

    /// Every field as it stood right after the last sync — what `patch()`
    /// compares against, so a save sends only what the owner changed.
    @State private var syncedPatch: ScheduleSetupViewModel.RulesPatch?

    private func syncUnlessEdited() {
        guard viewModel.rulesLoaded else { return }
        if synced, fieldsSignature != syncedSignature { return }
        sync()
    }

    private func sync() {
        synced = true
        defer {
            syncedSignature = fieldsSignature
            syncedPatch = fullPatch()
        }
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
        let setup = viewModel.teamSetup
        stays = setup.roleCloseMins.filter { $0.value > 0 }.mapValues(String.init)
        staysEdited = false
        // Blank shows the default as its placeholder; a cap the owner set
        // shows as typed.
        if let cap = setup.salariedCap, cap != setup.salariedCapDefault {
            salariedCap = RulesSalariedSection.hours(cap)
        } else {
            salariedCap = ""
        }
        capEdited = false
        floorNote = nil
    }

    /// Every field on the sheet as one body — the state `patch()` compares
    /// with the state at the last sync.
    private func fullPatch() -> ScheduleSetupViewModel.RulesPatch {
        var rules = parsedRules()
        rules["manager_on_duty"] = .bool(false)
        rules["keyholder_until_close"] = .bool(keyholderUntilClose)
        var arrivalMinutes: [String: Int?] = [:]
        for (role, text) in arrivals {
            if let n = Int(text.trimmingCharacters(in: .whitespaces)) { arrivalMinutes[role] = min(120, abs(n)) }
        }
        var p = ScheduleSetupViewModel.RulesPatch(rules: rules, roleFloors: cleanedFloors().mapValues { Optional($0) })
        p.jurisdiction = .some(jurisdiction.isEmpty ? nil : jurisdiction)
        p.roleArrivals = arrivalMinutes
        p.roleRequirements = requirements.filter { !$0.value.isEmpty }.mapValues { Optional($0) }
        p.fohRoles = fohRoles
        p.patioRoles = patioRoles
        var crossPercents: [String: Int?] = [:]
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
        var minutes: [String: Int?] = [:]
        for (role, text) in stays {
            if let n = Int(text.trimmingCharacters(in: .whitespaces)), n > 0 { minutes[role] = min(240, n) }
        }
        p.roleCloseMins = minutes
        let capText = salariedCap.trimmingCharacters(in: .whitespaces)
        p.salariedCap = .some(capText.isEmpty ? nil
                              : (NumberFormatter.cavnarDecimal.number(from: capText)?.doubleValue ?? Double(capText)))
        return p
    }

    /// Only what the owner changed since the rules loaded (schedule
    /// re-audit 10/4/26 UI-2). The sheet used to send everything on it, and
    /// the server took each key as the whole value: a value the owner never
    /// touched was re-sent (a default shown in a box became the owner's),
    /// and a sheet that failed to load sent blanks over every rule. A rule
    /// cleared goes back to its default (`rulesDefault`); a role cleared is
    /// sent as null; a role or rule untouched is not sent at all.
    private func patch() -> ScheduleSetupViewModel.RulesPatch {
        let now = fullPatch()
        var p = ScheduleSetupViewModel.RulesPatch()
        guard let base = syncedPatch else { return p }
        let a = base.rules ?? [:], b = now.rules ?? [:]
        var rules: [String: LooseValue] = [:]
        var reset: [String] = []
        for key in Set(a.keys).union(b.keys) where a[key] != b[key] {
            if let v = b[key] { rules[key] = v } else { reset.append(key) }
        }
        if !rules.isEmpty { p.rules = rules }
        if !reset.isEmpty { p.rulesDefault = reset.sorted() }
        p.roleFloors = Self.changedRoles(base.roleFloors, now.roleFloors)
        p.roleArrivals = Self.changedRoles(base.roleArrivals, now.roleArrivals)
        p.roleRequirements = Self.changedRoles(base.roleRequirements, now.roleRequirements)
        p.roleCrossTraining = Self.changedRoles(base.roleCrossTraining, now.roleCrossTraining)
        p.roleCloseMins = Self.changedRoles(base.roleCloseMins, now.roleCloseMins)
        if (now.jurisdiction ?? nil) != (base.jurisdiction ?? nil) { p.jurisdiction = now.jurisdiction }
        if now.fohRoles != base.fohRoles { p.fohRoles = now.fohRoles }
        if now.patioRoles != base.patioRoles { p.patioRoles = now.patioRoles }
        if now.trimToBudget != base.trimToBudget { p.trimToBudget = now.trimToBudget }
        if now.cutFloorDefault != base.cutFloorDefault { p.cutFloorDefault = now.cutFloorDefault }
        if (now.salariedCap ?? nil) != (base.salariedCap ?? nil) { p.salariedCap = now.salariedCap }
        // Already only when the owner changed the feed or typed a key.
        p.reservationProvider = now.reservationProvider
        p.reservationApiKey = now.reservationApiKey
        return p
    }

    /// The roles whose value differs between two maps: the new value, or
    /// null for a role the owner cleared. Nil when no role changed.
    nonisolated static func changedRoles<V: Equatable>(_ base: [String: V?]?, _ now: [String: V?]?) -> [String: V?]? {
        let a = base ?? [:], b = now ?? [:]
        var out: [String: V?] = [:]
        for key in Set(a.keys).union(b.keys) {
            let was: V? = a[key] ?? nil, isNow: V? = b[key] ?? nil
            // updateValue keeps a nil value (a cleared role) in the map.
            if was != isNow { out.updateValue(isNow, forKey: key) }
        }
        return out.isEmpty ? nil : out
    }

    /// True when a patch names nothing to change.
    nonisolated static func isEmpty(_ p: ScheduleSetupViewModel.RulesPatch) -> Bool {
        guard let data = try? JSONEncoder().encode(p) else { return false }
        return String(data: data, encoding: .utf8) == "{}"
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
        // A blank shortest shift is no rule — said, because the server
        // keeps a value a save does not send (an owner's edit never
        // vanishes behind an older screen).
        if out["min_shift_hours"] == nil { out["min_shift_hours"] = .null }
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
