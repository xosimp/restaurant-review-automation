import SwiftUI

/// Shift strength targets and shift leader requirements — the two rules
/// that turn Operational Scores into something the scheduler obeys.
///
/// A target is the scores of everyone in a role on one shift, added up:
/// two people at 5 make 10, and so do a 5, a 3 and a 2. A leader
/// requirement is stricter and names one person: "Saturday dinner needs a
/// bartender at 5."
///
/// Both are saved even when the current team cannot reach them, with a
/// warning — an owner setting a target may be describing the team they
/// mean to hire, not the one they have.
struct ShiftTargetsSection: View {
    @Bindable var viewModel: LaborViewModel
    var onExpand: (() -> Void)? = nil

    /// Edited locally and committed on Save, rather than written straight
    /// through — a half-typed "1" on the way to "10" is not a target, and
    /// posting it would warn about a number nobody meant.
    @State private var drafts: [String: String] = [:]
    @State private var rules: [ShiftLeaderRule] = []
    @State private var loadedFrom: [String: Double] = [:]
    @State private var showingAddRule = false
    @State private var savedFlash = false
    @FocusState private var focusedRole: String?

    var body: some View {
        CavnarDropdown(
            title: "Shift strength targets",
            subtitle: subtitle,
            isExpanded: $viewModel.targetsExpanded,
            onExpand: { onExpand?(); sync() }
        ) {
            VStack(alignment: .leading, spacing: 16) {
                if viewModel.team.isEmpty {
                    Text("Rate your team first — targets are built out of Operational Scores.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    thresholdEditor
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                    leaderRules
                    saveRow
                }
            }
            .contentShape(Rectangle())
            .onTapGesture { focusedRole = nil }
        }
        .onChange(of: viewModel.teamThresholds) { _, _ in sync() }
    }

    private var subtitle: String {
        let t = viewModel.teamThresholds.count
        let r = viewModel.leaderRules.count
        if t == 0 && r == 0 { return "No targets set" }
        var parts: [String] = []
        if t > 0 { parts.append(t == 1 ? "1 role target" : "\(t) role targets") }
        if r > 0 { parts.append(r == 1 ? "1 leader rule" : "\(r) leader rules") }
        return parts.joined(separator: " · ")
    }

    // MARK: Per-role targets

    private var thresholdEditor: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("The combined score of everyone in a role on one shift. Leave a role blank to set no target for it.")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(viewModel.teamRoles, id: \.self) { role in
                HStack(spacing: 10) {
                    Text(role)
                        .font(.cavnarBody(14.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    Spacer(minLength: 8)
                    if let best = reachable(role) {
                        Text("max \(best.formatted(.number.precision(.fractionLength(0))))")
                            .font(.cavnarNumber(12.5))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    TextField("—", text: binding(for: role))
                        .font(.cavnarNumber(15, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                        .multilineTextAlignment(.center)
                        .keyboardType(.decimalPad)
                        .focused($focusedRole, equals: role)
                        .frame(width: 62, height: 34)
                        .background(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .fill(Color.cavnarPaper2))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .strokeBorder(focusedRole == role ? Color.cavnarEmber : Color.cavnarPaper3,
                                              lineWidth: 1))
                }
            }

            ForEach(viewModel.targetWarnings, id: \.self) { warning in
                HStack(alignment: .top, spacing: 7) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.cavnarAmber)
                        .padding(.top, 2)
                    Text(warning)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    /// The best this role could ever total if everyone rated in it worked
    /// the same shift — shown beside the field so a target nobody can
    /// reach is obvious while it's being typed, not after saving.
    private func reachable(_ role: String) -> Double? {
        let scores = viewModel.team
            .filter { ($0.role ?? "").caseInsensitiveCompare(role) == .orderedSame }
            .compactMap { $0.score }
        return scores.isEmpty ? nil : Double(scores.reduce(0, +))
    }

    private func binding(for role: String) -> Binding<String> {
        Binding(get: { drafts[role] ?? "" }, set: { drafts[role] = $0 })
    }

    // MARK: Shift leader requirements

    private var leaderRules: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Shift leader requirements")
                .font(.cavnarBody(14.5, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            Text("Stricter than a target: this names one person who has to be on that shift.")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)

            ForEach(rules) { rule in
                HStack(alignment: .top, spacing: 10) {
                    Text(rule.sentence)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    Button {
                        Haptic.selection()
                        rules.removeAll { $0.id == rule.id }
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(Color.cavnarInk3)
                            .frame(width: 26, height: 26)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.vertical, 2)
            }

            Button {
                Haptic.light()
                showingAddRule = true
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "plus")
                        .font(.system(size: 11, weight: .bold))
                    Text("Add a requirement")
                        .font(.cavnarBody(14, weight: 600))
                }
                .foregroundStyle(Color.cavnarEmber)
            }
            .buttonStyle(.plain)
        }
        .sheet(isPresented: $showingAddRule) {
            LeaderRuleEditor(roles: viewModel.teamRoles) { rule in
                rules.append(rule)
            }
        }
    }

    // MARK: Save

    private var saveRow: some View {
        HStack(spacing: 12) {
            Button {
                focusedRole = nil
                Haptic.medium()
                Task {
                    await viewModel.saveTargets(parsedThresholds(), leaderRules: rules)
                    if viewModel.teamError == nil {
                        savedFlash = true
                        try? await Task.sleep(for: .seconds(2))
                        savedFlash = false
                    }
                }
            } label: {
                Group {
                    if viewModel.isSavingTargets {
                        CavnarShimmerText(text: "Saving", color: .cavnarPaper)
                    } else {
                        Text(savedFlash ? "Saved" : "Save targets")
                    }
                }
                .font(.cavnarBody(14.5, weight: 700))
                .foregroundStyle(Color.cavnarPaper)
                .frame(maxWidth: .infinity, minHeight: 42)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(Color.cavnarEmber))
            }
            .buttonStyle(.plain)
            .disabled(viewModel.isSavingTargets)
        }
    }

    /// Blank and unparseable fields drop out rather than saving a zero —
    /// "no target for this role" and "a target of nothing" are different,
    /// and the backend treats a missing role as unconstrained.
    private func parsedThresholds() -> [String: Double] {
        var out: [String: Double] = [:]
        for (role, text) in drafts {
            let trimmed = text.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, let value = Double(trimmed), value > 0 else { continue }
            out[role] = value
        }
        return out
    }

    /// Pull the server's saved values into the draft fields, but only when
    /// they've actually changed — re-syncing on every render would wipe
    /// whatever the owner is mid-way through typing.
    private func sync() {
        guard loadedFrom != viewModel.teamThresholds || drafts.isEmpty else { return }
        loadedFrom = viewModel.teamThresholds
        var next: [String: String] = [:]
        for role in viewModel.teamRoles {
            if let v = viewModel.teamThresholds.first(where: {
                $0.key.caseInsensitiveCompare(role) == .orderedSame
            })?.value {
                next[role] = v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v)
            }
        }
        drafts = next
        rules = viewModel.leaderRules
    }
}

/// One shift leader requirement, built in the owner's own words: which
/// role, which days, which part of the day, and how strong.
private struct LeaderRuleEditor: View {
    let roles: [String]
    let onAdd: (ShiftLeaderRule) -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var role = ""
    @State private var days: Set<String> = []
    @State private var daypart = ""
    @State private var minScore = 5.0
    @State private var count = 1

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    field("Role") {
                        chips(roles, selected: role) { role = $0 }
                    }
                    field("Days", hint: "Leave every day off to apply this to the whole week.") {
                        VStack(alignment: .leading, spacing: 8) {
                            chipRow(Array(LaborDayOfWeek.allNames.prefix(4)))
                            chipRow(Array(LaborDayOfWeek.allNames.suffix(3)))
                        }
                    }
                    field("Time of day") {
                        chips(["Any", "Morning", "Night"],
                              selected: daypart.isEmpty ? "Any" : daypart.capitalized) {
                            daypart = $0 == "Any" ? "" : $0.lowercased()
                        }
                    }
                    field("Minimum Operational Score") {
                        chips((1...5).map(String.init), selected: String(Int(minScore))) {
                            minScore = Double($0) ?? 5
                        }
                    }
                    field("How many people") {
                        chips((1...4).map(String.init), selected: String(count)) {
                            count = Int($0) ?? 1
                        }
                    }

                    if !role.isEmpty {
                        Text(preview.sentence)
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk2)
                            .padding(14)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(
                                RoundedRectangle(cornerRadius: 10, style: .continuous)
                                    .fill(Color.cavnarPaper2))
                    }
                }
                .padding(20)
            }
            .background(Color.cavnarPaper)
            .navigationTitle("New requirement")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") {
                        Haptic.success()
                        onAdd(preview)
                        dismiss()
                    }
                    .disabled(role.isEmpty)
                }
            }
        }
    }

    private var preview: ShiftLeaderRule {
        ShiftLeaderRule(role: role,
                        days: days.isEmpty ? nil : LaborDayOfWeek.allNames.filter { days.contains($0) },
                        daypart: daypart.isEmpty ? nil : daypart,
                        minScore: minScore, count: count)
    }

    private func field<Content: View>(_ label: String, hint: String? = nil,
                                      @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label.uppercased())
                .font(.cavnarBody(11.5, weight: 700))
                .tracking(0.8)
                .foregroundStyle(Color.cavnarInk3)
            if let hint {
                Text(hint)
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            content()
        }
    }

    private func chipRow(_ names: [String]) -> some View {
        HStack(spacing: 6) {
            ForEach(names, id: \.self) { day in
                chip(String(day.prefix(3)), on: days.contains(day)) {
                    if days.contains(day) { days.remove(day) } else { days.insert(day) }
                }
            }
        }
    }

    private func chips(_ options: [String], selected: String,
                       onPick: @escaping (String) -> Void) -> some View {
        HStack(spacing: 6) {
            ForEach(options, id: \.self) { option in
                chip(option, on: option == selected) { onPick(option) }
            }
        }
    }

    private func chip(_ label: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            Text(label)
                .font(.cavnarBody(13.5, weight: on ? 700 : 500))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 34)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? Color.cavnarEmber : Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
        }
        .buttonStyle(.plain)
    }
}
