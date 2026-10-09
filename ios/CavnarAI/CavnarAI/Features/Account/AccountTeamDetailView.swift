import SwiftUI

/// Opened from Account's "Manage team" row (owner-only — see AccountView's
/// gate on that row). A real second `users` login per teammate, not a
/// contacts list: invite creates an actual account tied to this same
/// restaurant_id with a temp password emailed directly, and revoke kills
/// that login's sessions immediately.
///
/// iOS readability round: one row per teammate — name and role — and a tap
/// opens that teammate's own sheet (role, what they see, the morning brief
/// and nightly report, Remove). It was every manager's "<Name> sees"
/// section stacked down one long page, with a remove chip on each row.
struct AccountTeamDetailView: View {
    let viewModel: AccountViewModel
    @State private var showingInvite = false
    @State private var openMember: TeamMemberRef?

    struct TeamMemberRef: Identifiable { let id: Int }

    /// The owner can open a teammate who isn't them.
    private func canOpen(_ member: TeamMember) -> Bool {
        viewModel.canEditTeamAccess && !member.isYou
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    AccountHero(title: "Team") {
                        GlowBadge(systemImage: "person.2", size: 64)
                    } subtitle: {
                        HomeMixedText.make("\(viewModel.teamMembers.count)"
                                           + (viewModel.teamMembers.count == 1 ? " login" : " logins"),
                                           role: .secondary)
                    }

                    if let error = viewModel.revokeTeamError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    }

                    AccountSection(kicker: "Logins") {
                        ForEach(Array(viewModel.teamMembers.enumerated()), id: \.element.id) { index, member in
                            let divider = index < viewModel.teamMembers.count - 1
                            if canOpen(member) {
                                Button {
                                    Haptic.light()
                                    openMember = TeamMemberRef(id: member.id)
                                } label: {
                                    AccountKVRow(label: member.displayName, showsDivider: divider) {
                                        HStack(spacing: CavnarSpace.xs) {
                                            AccountPill(text: member.roleLabel, on: member.role != "member")
                                            AccountDisclosureChip()
                                        }
                                    }
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                .accessibilityHint("Opens their role and what they see")
                            } else {
                                AccountKVRow(label: member.isYou ? "\(member.displayName) (you)" : member.displayName,
                                             showsDivider: divider) {
                                    AccountPill(text: member.roleLabel, on: member.role != "member")
                                }
                            }
                        }
                    }

                    if let error = viewModel.teamAccessError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
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
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Team")
            .task { await viewModel.loadTeam() }
            .sheet(isPresented: $showingInvite) {
                InviteTeamMemberSheet(viewModel: viewModel)
            }
            .sheet(item: $openMember) { ref in
                TeamMemberAccessSheet(viewModel: viewModel, memberId: ref.id)
            }
        }
    }
}

/// One teammate: their role, what they see beyond it, whether the morning
/// brief and the nightly report reach them, and Remove — each saved the
/// moment it changes, the sensitive ones confirmed first (co-owner, comps
/// and voids, remove).
private struct TeamMemberAccessSheet: View {
    let viewModel: AccountViewModel
    let memberId: Int
    @Environment(\.dismiss) private var dismiss
    @State private var pendingRevoke = false
    @State private var pendingLossGrant = false
    @State private var pendingCoOwner = false
    @State private var postedLabel: String?

    /// Live from the team list, so a change shows the server's answer.
    private var member: TeamMember? { viewModel.teamMembers.first { $0.id == memberId } }

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
                    .cavnarHitTarget()
                    .padding(.vertical, -8)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(label): \(on ? "on" : "off")")
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    if let member {
                        AccountHero(title: member.displayName) {
                            GlowBadge(systemImage: "person", size: 56)
                        } subtitle: {
                            Text(member.roleLabel)
                        }

                        if member.roleEditable ?? false {
                            AccountSection(kicker: "Role") {
                                AccountKVRow(label: "Role", showsDivider: false) {
                                    Menu {
                                        ForEach(viewModel.teamRoleOptions) { option in
                                            Button {
                                                if option.key == "client" {
                                                    pendingCoOwner = true
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
                                        HStack(spacing: 6) {
                                            Text(member.roleLabel).cavnarText(.label, color: .cavnarEmber2)
                                            Image(systemName: "chevron.up.chevron.down")
                                                .font(.cavnar(.caption))
                                                .foregroundStyle(Color.cavnarEmber2)
                                                .accessibilityHidden(true)
                                        }
                                        .cavnarHitTarget()
                                        .padding(.vertical, -7)
                                    }
                                    .accessibilityLabel("Role for \(member.displayName): \(member.roleLabel)")
                                }
                            }
                        }

                        // What this manager sees beyond their role, and
                        // whether the morning brief reaches them.
                        if member.accessGrantable ?? false {
                            AccountSection(kicker: "What they see") {
                                ForEach(viewModel.teamAccessOptions) { option in
                                    accessRow(label: option.label,
                                              on: (member.access ?? []).contains(option.key),
                                              showsDivider: true) { newValue in
                                        if option.key == "loss.view" && newValue {
                                            pendingLossGrant = true
                                            return
                                        }
                                        Task { await viewModel.setTeamAccess(member.id, permission: option.key, enabled: newValue) }
                                    }
                                }
                                accessRow(label: "Morning brief", on: member.morningBrief ?? false,
                                          showsDivider: true) { newValue in
                                    Task { await viewModel.setTeamAccess(member.id, morningBrief: newValue) }
                                }
                                // The nightly Daily Sales Report — on for every
                                // manager unless the owner leaves them off it
                                // (the web's "Nightly report", parity #70).
                                accessRow(label: "Nightly report", on: member.nightlyReport ?? true,
                                          showsDivider: false) { newValue in
                                    Task { await viewModel.setTeamAccess(member.id, nightlyReport: newValue) }
                                }
                            }
                        }

                        if let error = viewModel.teamAccessError {
                            Text(error).cavnarText(.secondary, color: .cavnarRedText)
                        }
                        if let error = viewModel.revokeTeamError {
                            Text(error).cavnarText(.secondary, color: .cavnarRedText)
                        }

                        VStack(alignment: .leading, spacing: 0) {
                            AccountActionRow(label: "Remove from the team",
                                             detail: "Signs them out and ends this login.",
                                             symbol: "xmark", tone: .cavnarRed, showsDivider: false) {
                                pendingRevoke = true
                            }
                        }
                        .accountCard()
                    } else {
                        Text("They're no longer on the team.").cavnarText(.body)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome(member?.displayName ?? "Teammate")
            .confirmationDialog(
                member.map { "Remove \($0.displayName)?" } ?? "",
                isPresented: $pendingRevoke,
                titleVisibility: .visible
            ) {
                Button("Remove access", role: .destructive) {
                    guard let member else { return }
                    Task {
                        if await viewModel.revokeTeamMember(member.id) {
                            Haptic.success()
                            dismiss()
                        }
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("They'll be signed out immediately and won't be able to log back in.")
            }
            .confirmationDialog(
                member.map { "Make \($0.displayName) a co-owner?" } ?? "",
                isPresented: $pendingCoOwner,
                titleVisibility: .visible
            ) {
                Button("Make co-owner") {
                    guard let member else { return }
                    Task {
                        if await viewModel.setTeamRole(member.id, role: "client") {
                            Haptic.success()
                            postedLabel = "\(member.displayName) is a co-owner"
                        }
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("They'll be able to do everything you can — manage the team, settings and every number.")
            }
            .confirmationDialog(
                member.map { "Show comps & voids to \($0.displayName)?" } ?? "",
                isPresented: $pendingLossGrant,
                titleVisibility: .visible
            ) {
                Button("Turn on") {
                    guard let member else { return }
                    Task { await viewModel.setTeamAccess(member.id, permission: "loss.view", enabled: true) }
                }
                Button("Cancel", role: .cancel) {}
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
    /// Added — the form is spent, so nothing on it is "unsaved" any more.
    @State private var added = false
    /// Co-owner picked: confirmed first, as changing an existing teammate's
    /// role to co-owner is (re-audit M13).
    @State private var pendingCoOwner = false
    @State private var confirmingCancel = false
    @FocusState private var focusedField: InviteField?

    private let roleChoices: [(key: String, label: String, hint: String)] = [
        ("manager", "Manager", "Sees what you open"),
        ("client", "Co-owner", "Everything"),
        ("member", "Teammate", "Basic"),
    ]

    private var canSubmit: Bool {
        !added && !viewModel.isInvitingTeamMember && !name.trimmingCharacters(in: .whitespaces).isEmpty && email.contains("@")
    }

    /// Something typed and not yet sent: Back and swipe-down ask first (M9).
    private var isDirty: Bool {
        !added && (!name.trimmingCharacters(in: .whitespaces).isEmpty || !email.trimmingCharacters(in: .whitespaces).isEmpty)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    Text("They'll get an email with a temporary password and can set their own once they sign in.")
                        .cavnarText(.body)

                    AccountField(label: "Name", text: $name, focus: $focusedField, field: .name)
                    AccountField(label: "Email", text: $email, focus: $focusedField, field: .email, keyboardType: .emailAddress, showsDivider: false)

                    AccountSection(kicker: "Role") {
                        ForEach(Array(roleChoices.enumerated()), id: \.element.key) { index, choice in
                            Button {
                                Haptic.light()
                                if choice.key == "client" && role != "client" {
                                    pendingCoOwner = true
                                } else {
                                    role = choice.key
                                }
                            } label: {
                                // The hint stays visible when picked; a check
                                // marks the choice (it was replaced by "Selected").
                                AccountKVRow(label: choice.label, showsDivider: index < roleChoices.count - 1) {
                                    HStack(spacing: CavnarSpace.xs) {
                                        AccountPill(text: choice.hint, on: role == choice.key)
                                        Image(systemName: role == choice.key ? "checkmark.circle.fill" : "circle")
                                            .font(.cavnar(.body))
                                            .foregroundStyle(role == choice.key ? Color.cavnarEmber : Color.cavnarInk3)
                                            .accessibilityHidden(true)
                                    }
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityAddTraits(role == choice.key ? .isSelected : [])
                        }
                    }

                    if let error = viewModel.inviteTeamError {
                        Text(error).cavnarText(.body, color: .cavnarRedText)
                    }
                    // Added, but the email with their sign-in didn't go out:
                    // the server's sentence, not "Invite sent" (re-audit A3).
                    if added, let notice = viewModel.inviteEmailNotice {
                        Text(notice)
                            .cavnarText(.body, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    VStack(spacing: 10) {
                        if added {
                            Button {
                                dismiss()
                            } label: {
                                Text("Done").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle())
                        } else {
                            Button {
                                Task {
                                    if await viewModel.inviteTeamMember(name: name, email: email, role: role) {
                                        added = true
                                        if viewModel.inviteEmailNotice == nil {
                                            Haptic.success()
                                            postedLabel = "Invite sent"
                                        } else {
                                            Haptic.warning()
                                        }
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
                                if isDirty { confirmingCancel = true } else { dismiss() }
                            } label: {
                                Text("Cancel").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                    }
                    .padding(.top, 6)
                }
                .padding(20)
            }
            .accountSheetChrome("Invite a teammate", isDirty: isDirty)
            .keyboardNavToolbar($focusedField)
            .cavnarPostedOverlay(postedLabel) { dismiss() }
            .confirmationDialog("Invite them as a co-owner?", isPresented: $pendingCoOwner, titleVisibility: .visible) {
                Button("Co-owner") { Haptic.selection(); role = "client" }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("They'll be able to do everything you can \u{2014} manage the team, settings and every number.")
            }
            .confirmationDialog("Discard changes?", isPresented: $confirmingCancel, titleVisibility: .visible) {
                Button("Discard changes", role: .destructive) { dismiss() }
                Button("Keep editing", role: .cancel) {}
            } message: {
                Text("What you changed here hasn\u{2019}t been saved.")
            }
        }
    }
}
