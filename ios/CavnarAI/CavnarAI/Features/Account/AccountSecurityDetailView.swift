import SwiftUI

/// Opened from Account's "Security & devices" row. Was four cards of
/// unrelated heights (Sign-in, 2FA, a single-toggle Alerts card, Active
/// Devices) with no visual relationship between them — collapsed to two:
/// everything about how you authenticate in one "Sign-in & security" card
/// (username, password, 2FA, sign-in notifications), device management
/// in the other. A lone toggle in its own full-width card was most of
/// the "zero flow" feeling on its own.
struct AccountSecurityDetailView: View {
    let viewModel: AccountViewModel
    let account: AccountInfo
    @State private var showingChangePassword = false
    @State private var showing2FASetup = false

    var body: some View {
        NavigationStack {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("Sign-in & security")
                    VStack(alignment: .leading, spacing: 14) {
                        HStack {
                            Text("Username").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                            Spacer()
                            Text(account.username).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                        }
                        Button("Change password") { Haptic.light(); showingChangePassword = true }
                            .font(.cavnarBody(13, weight: 600))
                            .foregroundStyle(Color.cavnarEmber)

                        divider()

                        if account.twoFAEnabled {
                            HStack {
                                Text("Two-factor authentication").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                                Spacer()
                                Text("On").font(.cavnarBody(12, weight: 700)).foregroundStyle(Color.cavnarGreen)
                            }
                            Button("Disable two-factor authentication", role: .destructive) {
                                Task { await viewModel.disable2FA() }
                            }
                            .font(.cavnarBody(13, weight: 600))
                        } else {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Two-factor authentication").font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                                Text("Adds an email code on new sign-ins.").font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                            }
                            Button("Enable two-factor authentication") { Haptic.light(); showing2FASetup = true }
                                .font(.cavnarBody(13, weight: 600))
                                .foregroundStyle(Color.cavnarEmber)
                        }

                        divider()

                        // No manual Haptic.selection() — Toggle/UISwitch
                        // already fires its own automatic system haptic.
                        Toggle(isOn: Binding(
                            get: { account.loginNotify },
                            set: { newValue in Task { await viewModel.toggleLoginNotify(newValue) } }
                        )) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Sign-in notifications").font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                                Text("Get notified of new sign-ins to your account").font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        .tint(Color.cavnarEmber)
                    }
                    .cavnarCard()
                }

                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("Active devices")
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(viewModel.sessions) { session in
                            VStack(alignment: .leading, spacing: 2) {
                                HStack {
                                    Text(session.label).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                                    if session.isCurrent {
                                        Text("This device")
                                            .font(.cavnarBody(10, weight: 700))
                                            .foregroundStyle(Color.cavnarEmber)
                                    }
                                }
                                Text("Last active \(session.lastActive)")
                                    .font(.cavnarBody(11))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            if session.id != viewModel.sessions.last?.id {
                                divider()
                            }
                        }
                        Button("Sign out all other devices", role: .destructive) {
                            Task { await viewModel.revokeOtherSessions() }
                        }
                        .font(.cavnarBody(13, weight: 600))
                    }
                    .cavnarCard()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .cavnarModuleBackground()
        .navigationTitle("Security")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showingChangePassword) {
            ChangePasswordSheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showing2FASetup) {
            TwoFactorSetupSheet(viewModel: viewModel)
        }
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.cavnarBody(12.5, weight: 700))
            .foregroundStyle(Color.cavnarInk3)
    }

    private func divider() -> some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }
}

private enum ChangePasswordField: Hashable, CaseIterable {
    case current, newPassword
}

private struct ChangePasswordSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var current = ""
    @State private var newPassword = ""
    @FocusState private var focusedField: ChangePasswordField?

    private var canSubmit: Bool {
        !viewModel.isChangingPassword && !current.isEmpty && newPassword.count >= 8
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    CavnarFloatingField(
                        icon: "lock", placeholder: "Current password", text: $current,
                        isSecure: true, textContentType: .password,
                        focus: $focusedField, field: .current
                    )
                    CavnarFloatingField(
                        icon: "lock.fill", placeholder: "New password (8+ characters)", text: $newPassword,
                        isSecure: true, textContentType: .newPassword,
                        focus: $focusedField, field: .newPassword
                    )

                    if let error = viewModel.changePasswordError {
                        Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                    }

                    CavnarFormButtonPair { matchedWidth in
                        Button {
                            Task {
                                await viewModel.changePassword(current: current, newPassword: newPassword)
                                if viewModel.changePasswordSucceeded { dismiss() }
                            }
                        } label: {
                            if viewModel.isChangingPassword {
                                ProgressView().tint(Color.cavnarInk)
                            } else {
                                Text("Change password")
                            }
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit, matchedWidth: matchedWidth))
                        .disabled(!canSubmit)
                    } cancelAction: {
                        dismiss()
                    }
                    .padding(.top, 6)
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Change Password")
            .navigationBarTitleDisplayMode(.inline)
            .keyboardNavToolbar($focusedField)
        }
    }
}

private enum TwoFactorSetupField: Hashable, CaseIterable {
    case code
}

private struct TwoFactorSetupSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var code = ""
    @FocusState private var focusedField: TwoFactorSetupField?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    if let masked = viewModel.twoFATestMasked {
                        Text("Code sent to \(masked)")
                            .font(.cavnarBody(12))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.top, -14)

                        CavnarFloatingField(
                            icon: "lock.shield", placeholder: "6-digit code", text: $code,
                            keyboardType: .numberPad,
                            focus: $focusedField, field: .code
                        )

                        if let error = viewModel.twoFAError {
                            Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                        }

                        CavnarFormButtonPair { matchedWidth in
                            Button {
                                Task {
                                    if await viewModel.verify2FA(code: code) { dismiss() }
                                }
                            } label: {
                                if viewModel.is2FABusy {
                                    ProgressView().tint(Color.cavnarInk)
                                } else {
                                    Text("Verify and enable")
                                }
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.is2FABusy || code.count != 6, matchedWidth: matchedWidth))
                            .disabled(viewModel.is2FABusy || code.count != 6)
                        } cancelAction: {
                            dismiss()
                        }
                        .padding(.top, 6)
                    } else {
                        Text("We'll text a 6-digit code to the phone number on file to confirm two-factor sign-in works before turning it on.")
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk3)

                        if let error = viewModel.twoFAError {
                            Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                        }

                        CavnarFormButtonPair { matchedWidth in
                            Button {
                                Task { await viewModel.send2FATest() }
                            } label: {
                                if viewModel.is2FABusy {
                                    ProgressView().tint(Color.cavnarInk)
                                } else {
                                    Text("Send test code")
                                }
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.is2FABusy, matchedWidth: matchedWidth))
                            .disabled(viewModel.is2FABusy)
                        } cancelAction: {
                            dismiss()
                        }
                        .padding(.top, 6)
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Enable 2FA")
            .navigationBarTitleDisplayMode(.inline)
            // Single field, nothing to step between — checkmark only,
            // matching Apple's own single-field bar rather than showing
            // two permanently-disabled chevrons for show.
            .keyboardDoneToolbar { focusedField = nil }
        }
    }
}
