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
                                    AccountPill(text: member.role == "member" ? "Member" : "Owner", on: member.role != "member")
                                    if !member.isYou && member.role == "member" {
                                        AccountLink(title: "Remove", tone: .cavnarRed) {
                                            pendingRevoke = member
                                        }
                                    }
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
    @State private var postedLabel: String?
    @FocusState private var focusedField: InviteField?

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

                    if let error = viewModel.inviteTeamError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.inviteTeamMember(name: name, email: email) {
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
