import SwiftUI

/// Roster & rules → Roles and job codes (schedule audit 10/3/26 D-13): the
/// job codes the POS uses, grouped into roles — "Server AM" and "Server PM"
/// are both Server — so a rule, a floor, a closer or a leader rule set on
/// a role holds on every one of its codes. Suggested from the job names
/// until the owner saves their own; only the account owner saves.
struct RoleFamiliesSheet: View {
    @Bindable var store: TeamSetupStore

    /// The role each job code belongs to, as the owner is editing it.
    @State private var draft: [String: String] = [:]
    @State private var seeded = false
    @State private var posted: String?
    @State private var newRoleFor: String?
    @State private var newRoleName = ""

    private var families: RoleFamilies? { store.families }
    private var canEdit: Bool { families?.canEdit ?? false }

    /// The roles in the draft, each with its codes, in name order.
    private var groups: [(role: String, codes: [String])] {
        var byRole: [String: [String]] = [:]
        for (code, role) in draft { byRole[role, default: []].append(code) }
        return byRole.keys.sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
            .map { ($0, byRole[$0]!.sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }) }
    }

    private var changed: Bool { draft != (families?.assignment ?? [:]) }
    private var isOwners: Bool { !(families?.stored.isEmpty ?? true) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("The job codes your POS uses, grouped into roles. A floor, a closer, a leader rule or a staffing rule set on a role holds on every one of its codes \u{2014} the lunch code and the dinner code are halves of one role.")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    if store.isLoadingFamilies && families == nil {
                        CavnarSkeletonBar(height: 3)
                            .padding(.vertical, 10)
                            .accessibilityLabel("Loading the roles")
                    } else if let error = store.familiesError, families == nil {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                    } else if families != nil {
                        HStack(spacing: 6) {
                            AccountChip(text: isOwners ? "Yours" : "Suggested from the job names", muted: true)
                        }
                        if groups.isEmpty {
                            Text("No job codes on file yet \u{2014} they come from your shift history.")
                                .font(.cavnar(.secondary))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        ForEach(groups, id: \.role) { group in
                            AccountSection(kicker: group.role) {
                                ForEach(Array(group.codes.enumerated()), id: \.element) { index, code in
                                    AccountKVRow(label: code, showsDivider: index < group.codes.count - 1) {
                                        if canEdit {
                                            SetupSelectMenu(current: "Move",
                                                            options: moveOptions(for: code, from: group.role)) { target in
                                                if target == "\u{0}new" {
                                                    newRoleFor = code
                                                    newRoleName = ""
                                                } else {
                                                    draft[code] = target
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        if let code = newRoleFor {
                            newRoleCard(code)
                        }
                        if let error = store.familiesError {
                            Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if canEdit {
                            Button {
                                Task {
                                    if await store.saveFamilies(draft) {
                                        seeded = false
                                        seed()
                                        posted = "Roles saved"
                                    }
                                }
                            } label: {
                                Group {
                                    if store.isSavingFamilies { CavnarShimmerText(text: "Saving\u{2026}") }
                                    else { Text(isOwners || changed ? "Save these roles" : "Confirm these roles") }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: store.isSavingFamilies || draft.isEmpty))
                            .disabled(store.isSavingFamilies || draft.isEmpty)
                            if isOwners {
                                Button {
                                    Task {
                                        if await store.saveFamilies([:]) {
                                            seeded = false
                                            seed()
                                            posted = "Back to the suggestion"
                                        }
                                    }
                                } label: {
                                    Text("Go back to the suggestion")
                                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                        .foregroundStyle(Color.cavnarEmber2)
                                        .frame(maxWidth: .infinity, minHeight: 36)
                                }
                                .buttonStyle(.plain)
                                .disabled(store.isSavingFamilies)
                            }
                        } else {
                            Text("Only the account owner changes the roles.")
                                .font(.cavnar(.secondary))
                                .foregroundStyle(Color.cavnarAmber)
                        }
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Roles and job codes")
            .cavnarPostedOverlay(posted) { posted = nil }
        }
        .task {
            await store.loadFamilies()
            seed()
        }
    }

    private func seed() {
        guard !seeded, let f = families else { return }
        seeded = true
        draft = f.assignment
    }

    private func moveOptions(for code: String, from role: String) -> [(value: String, label: String)] {
        var out: [(value: String, label: String)] = groups.map(\.role).filter { $0 != role }.map { ($0, "Into \($0)") }
        if code != role { out.append((code, "Its own role")) }
        out.append(("\u{0}new", "A new role\u{2026}"))
        return out
    }

    private func newRoleCard(_ code: String) -> some View {
        AccountSection(kicker: "A new role for \(code)") {
            VStack(alignment: .leading, spacing: 10) {
                TextField("Role name, e.g. Server", text: $newRoleName)
                    .cavnarTextFieldStyle()
                    .textInputAutocapitalization(.words)
                    .autocorrectionDisabled()
                HStack(spacing: 10) {
                    Button {
                        let name = newRoleName.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !name.isEmpty else { return }
                        draft[code] = name
                        newRoleFor = nil
                    } label: { Text("Move it there").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: newRoleName.trimmingCharacters(in: .whitespaces).isEmpty))
                        .disabled(newRoleName.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button {
                        newRoleFor = nil
                    } label: {
                        Text("Cancel").font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarInk3)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 10)
        }
    }
}
