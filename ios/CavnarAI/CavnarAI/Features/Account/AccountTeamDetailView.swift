import SwiftUI

/// Opened from Account's "Manage team" row (owner-only — see AccountView's
/// gate on that row). A real second `users` login per teammate, not a
/// contacts list: invite creates an actual account tied to this same
/// restaurant_id with a temp password emailed directly, and revoke kills
/// that login's sessions immediately.
struct AccountTeamDetailView: View {
    let viewModel: AccountViewModel
    @State private var showingInvite = false
    @State private var pendingRevoke: TeamMember?
    @State private var postedLabel: String?
    @State private var pendingLossGrant: TeamMember?
    @State private var pendingCoOwner: TeamMember?

    /// One on/off setting: a tappable pill, not a native Toggle (its height
    /// breaks the kit's row rhythm — see AccountSheetKit).
    private func accessRow(label: String, on: Bool, showsDivider: Bool,
                           set: @escaping (Bool) -> Void) -> some View {
        AccountKVRow(label: label, showsDivider: showsDivider) {
            Button {
                Haptic.light()
                set(!on)
            } label: {
                AccountPill(text: on ? "On" : "Off", on: on)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(label): \(on ? "on" : "off")")
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Team") {
                        GlowBadge(systemImage: "person.2", size: 64)
                    } subtitle: {
                        Text("\(viewModel.teamMembers.count)").font(.cavnarNumber(15.5, weight: 600))
                            + Text(viewModel.teamMembers.count == 1 ? " login" : " logins")
                    }

                    if let error = viewModel.revokeTeamError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    AccountSection(kicker: "Who has access") {
                        ForEach(Array(viewModel.teamMembers.enumerated()), id: \.element.id) { index, member in
                            AccountKVRow(label: member.username, showsDivider: index < viewModel.teamMembers.count - 1) {
                                HStack(spacing: 8) {
                                    if viewModel.canEditTeamAccess && (member.roleEditable ?? false) {
                                        Menu {
                                            ForEach(viewModel.teamRoleOptions) { option in
                                                Button {
                                                    if option.key == "client" {
                                                        pendingCoOwner = member
                                                    } else {
                                                        Task { await viewModel.setTeamRole(member.id, role: option.key) }
                                                    }
                                                } label: {
                                                    if option.key == member.role {
                                                        Label(option.label, systemImage: "checkmark")
                                                    } else {
                                                        Text(option.label)
                                                    }
                                                }
                                            }
                                        } label: {
                                            AccountPill(text: member.roleLabel, on: member.role != "member")
                                        }
                                        .accessibilityLabel("Role for \(member.username): \(member.roleLabel)")
                                    } else {
                                        AccountPill(text: member.roleLabel, on: member.role != "member")
                                    }
                                    if !member.isYou && viewModel.canEditTeamAccess {
                                        AccountActionChip(symbol: "xmark", tone: .cavnarRed, accessibilityLabel: "Remove \(member.username)") {
                                            pendingRevoke = member
                                        }
                                    }
                                }
                            }
                        }
                    }

                    if let error = viewModel.teamAccessError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    // What each manager sees beyond their role, and whether
                    // the morning brief reaches them. Owner only.
                    if viewModel.canEditTeamAccess {
                        ForEach(viewModel.teamMembers.filter { ($0.accessGrantable ?? false) && !$0.isYou }) { member in
                            AccountSection(kicker: "\(member.username) sees") {
                                ForEach(viewModel.teamAccessOptions) { option in
                                    accessRow(label: option.label,
                                              on: (member.access ?? []).contains(option.key),
                                              showsDivider: true) { newValue in
                                        if option.key == "loss.view" && newValue {
                                            pendingLossGrant = member
                                            return
                                        }
                                        Task { await viewModel.setTeamAccess(member.id, permission: option.key, enabled: newValue) }
                                    }
                                }
                                accessRow(label: "Morning brief", on: member.morningBrief ?? false,
                                          showsDivider: false) { newValue in
                                    Task { await viewModel.setTeamAccess(member.id, morningBrief: newValue) }
                                }
                            }
                        }
                    }

                    Button {
                        Haptic.light()
                        showingInvite = true
                    } label: {
                        Text("Invite a team member").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Team")
            .task { await viewModel.loadTeam() }
            .sheet(isPresented: $showingInvite) {
                InviteTeamMemberSheet(viewModel: viewModel)
            }
            .confirmationDialog(
                pendingRevoke.map { "Remove \($0.username)?" } ?? "",
                isPresented: Binding(get: { pendingRevoke != nil }, set: { if !$0 { pendingRevoke = nil } }),
                titleVisibility: .visible
            ) {
                Button("Remove access", role: .destructive) {
                    guard let member = pendingRevoke else { return }
                    Task {
                        if await viewModel.revokeTeamMember(member.id) {
                            Haptic.success()
                            postedLabel = "\(member.username) removed"
                        }
                        pendingRevoke = nil
                    }
                }
                Button("Cancel", role: .cancel) { pendingRevoke = nil }
            } message: {
                Text("They'll be signed out immediately and won't be able to log back in.")
            }
            .confirmationDialog(
                pendingCoOwner.map { "Make \($0.username) a co-owner?" } ?? "",
                isPresented: Binding(get: { pendingCoOwner != nil }, set: { if !$0 { pendingCoOwner = nil } }),
                titleVisibility: .visible
            ) {
                Button("Make co-owner") {
                    guard let member = pendingCoOwner else { return }
                    Task {
                        if await viewModel.setTeamRole(member.id, role: "client") {
                            Haptic.success()
                            postedLabel = "\(member.username) is a co-owner"
                        }
                        pendingCoOwner = nil
                    }
                }
                Button("Cancel", role: .cancel) { pendingCoOwner = nil }
            } message: {
                Text("They'll be able to do everything you can — manage the team, settings and every number.")
            }
            .confirmationDialog(
                pendingLossGrant.map { "Show comps & voids to \($0.username)?" } ?? "",
                isPresented: Binding(get: { pendingLossGrant != nil }, set: { if !$0 { pendingLossGrant = nil } }),
                titleVisibility: .visible
            ) {
                Button("Turn on") {
                    guard let member = pendingLossGrant else { return }
                    Task {
                        await viewModel.setTeamAccess(member.id, permission: "loss.view", enabled: true)
                        pendingLossGrant = nil
                    }
                }
                Button("Cancel", role: .cancel) { pendingLossGrant = nil }
            } message: {
                Text("These patterns can name the manager who approved the comps — including this person.")
            }
            .cavnarPostedOverlay(postedLabel, onFinished: { postedLabel = nil })
        }
    }
}

private enum InviteField: Hashable, CaseIterable { case name, email }

private struct InviteTeamMemberSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var email = ""
    @State private var role = "manager"
    @State private var postedLabel: String?
    @FocusState private var focusedField: InviteField?

    private let roleChoices: [(key: String, label: String, hint: String)] = [
        ("manager", "Manager", "Sees what you open"),
        ("client", "Co-owner", "Everything"),
        ("member", "Teammate", "Basic"),
    ]

    private var canSubmit: Bool {
        !viewModel.isInvitingTeamMember && !name.trimmingCharacters(in: .whitespaces).isEmpty && email.contains("@")
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    Text("They'll get an email with a temporary password and can set their own once they sign in.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)

                    AccountField(label: "Name", text: $name, focus: $focusedField, field: .name)
                    AccountField(label: "Email", text: $email, focus: $focusedField, field: .email, keyboardType: .emailAddress, showsDivider: false)

                    AccountSection(kicker: "Role") {
                        ForEach(Array(roleChoices.enumerated()), id: \.element.key) { index, choice in
                            Button {
                                Haptic.light()
                                role = choice.key
                            } label: {
                                AccountKVRow(label: choice.label, showsDivider: index < roleChoices.count - 1) {
                                    AccountPill(text: role == choice.key ? "Selected" : choice.hint, on: role == choice.key)
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }

                    if let error = viewModel.inviteTeamError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.inviteTeamMember(name: name, email: email, role: role) {
                                    Haptic.success()
                                    postedLabel = "Invite sent"
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isInvitingTeamMember {
                                    CavnarShimmerText(text: "Adding…", color: Color.cavnarInk)
                                } else {
                                    Text("Add teammate")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                        .disabled(!canSubmit)

                        Button {
                            dismiss()
                        } label: {
                            Text("Cancel").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    .padding(.top, 6)
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Invite Team Member")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Invite Team Member") }
            .keyboardNavToolbar($focusedField)
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}
