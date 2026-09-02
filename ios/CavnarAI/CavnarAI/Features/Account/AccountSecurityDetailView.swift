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
    @State private var disabledLabel: String?
    @Environment(SessionStore.self) private var sessionStore

    var body: some View {
        NavigationStack {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 4) {
                    sectionHeader("Sign-in & security")
                    VStack(alignment: .leading, spacing: 14) {
                        HStack {
                            Text("Username").font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
                            Spacer()
                            Text(account.username).font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                        }
                        Button("Change password") { Haptic.light(); showingChangePassword = true }
                            .font(.cavnarBody(14.5, weight: 600))
                            .foregroundStyle(Color.cavnarEmber)

                        divider()

                        if account.twoFAEnabled {
                            HStack {
                                Text("Two-factor authentication").font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
                                Spacer()
                                Text("On").font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarGreen)
                            }
                            Button("Disable two-factor authentication", role: .destructive) {
                                Haptic.light()
                                Task {
                                    if await viewModel.disable2FA() {
                                        Haptic.success()
                                        disabledLabel = "Two-factor disabled"
                                    }
                                }
                            }
                            .font(.cavnarBody(14.5, weight: 600))
                        } else {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Two-factor authentication").font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                                Text("Adds an email code on new sign-ins.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            }
                            Button("Enable two-factor authentication") { Haptic.light(); showing2FASetup = true }
                                .font(.cavnarBody(14.5, weight: 600))
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
                                Text("Sign-in notifications").font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                                Text("Get notified of new sign-ins to your account").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
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
                                    Text(session.label).font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                                    if session.isCurrent {
                                        Text("This device")
                                            .font(.cavnarBody(14, weight: 700))
                                            .foregroundStyle(Color.cavnarEmber)
                                    }
                                }
                                Text("Last active \(session.lastActive)")
                                    .font(.cavnarBody(14))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            if session.id != viewModel.sessions.last?.id {
                                divider()
                            }
                        }
                        Button("Sign out all other devices", role: .destructive) {
                            Task { await viewModel.revokeOtherSessions() }
                        }
                        .font(.cavnarBody(14.5, weight: 600))
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
        .toolbar { cavnarTitleToolbar("Security") }
        .sheet(isPresented: $showingChangePassword) {
            ChangePasswordSheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showing2FASetup) {
            TwoFactorSetupSheet(viewModel: viewModel)
        }
        // Same resume as AccountView's own — this sheet was itself torn
        // down by the relock, so it re-presents its own child sheet too.
        // Deliberately delayed, not fired synchronously in onAppear: this
        // view is ITSELF still mid-presentation (AccountView is animating
        // it in as a sheet) at the moment onAppear fires. Presenting a
        // SECOND sheet from on top of that before the first transition has
        // actually settled is what produced the garbled oversized-button
        // render a device test caught — SwiftUI's sheet-presentation
        // animation doesn't like being interrupted by another sheet
        // request mid-flight. Waiting past the standard ~0.35s sheet
        // transition lets this screen fully settle first.
        .task {
            guard sessionStore.pendingTwoFactorSetupEmail != nil else { return }
            try? await Task.sleep(for: .milliseconds(450))
            showing2FASetup = true
        }
        .cavnarPostedOverlay(disabledLabel) { disabledLabel = nil }
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.cavnarBody(14.5, weight: 700))
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
    @State private var postedLabel: String?
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
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }

                    CavnarFormButtonPair { matchedWidth in
                        Button {
                            Task {
                                await viewModel.changePassword(current: current, newPassword: newPassword)
                                if viewModel.changePasswordSucceeded {
                                    Haptic.success()
                                    postedLabel = "Password changed"
                                }
                            }
                        } label: {
                            if viewModel.isChangingPassword {
                                CavnarShimmerText(text: "Changing…", color: Color.cavnarInk)
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
            .toolbar { cavnarTitleToolbar("Change Password") }
            .keyboardNavToolbar($focusedField)
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}

private struct TwoFactorSetupSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss
    @Environment(SessionStore.self) private var sessionStore
    @State private var code = ""
    @State private var postedLabel: String?
    @FocusState private var isCodeFocused: Bool
    // "email" or "sms" — which channel the test code goes out on. Text is
    // only offered when a phone number is on file (see hasPhone below);
    // otherwise this stays "email" and the picker never renders.
    @State private var selectedMethod: String = "email"

    private var hasPhone: Bool {
        !(viewModel.summary?.profile.ownerPhone?.trimmingCharacters(in: .whitespaces).isEmpty ?? true)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    if let masked = viewModel.twoFATestMasked {
                        Text("Code sent to \(masked)")
                            .font(.cavnarBody(16))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.top, -14)

                        // Six cells, not a bare field — each digit pops
                        // into place, the active cell carries a blinking
                        // ember caret, the row warms while verifying, and a
                        // wrong code shakes it red. Submits itself the
                        // moment the sixth digit lands, same as sign-in's
                        // TwoFactorView; the button below stays as an
                        // explicit fallback.
                        CavnarCodeEntry(
                            code: $code,
                            isVerifying: viewModel.is2FABusy,
                            isError: viewModel.twoFAError != nil,
                            focus: $isCodeFocused
                        )
                        .onChange(of: code) { _, new in
                            if new.count < 6 { viewModel.twoFAError = nil }
                            if new.count == 6, !viewModel.is2FABusy {
                                Task {
                                    if await viewModel.verify2FA(code: code) {
                                        Haptic.success()
                                        postedLabel = "Two-factor enabled"
                                        sessionStore.pendingTwoFactorSetupEmail = nil
                                    }
                                }
                            }
                        }
                        .onAppear { isCodeFocused = true }

                        if let error = viewModel.twoFAError {
                            Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                        }

                        // Plain full-width buttons, not CavnarFormButtonPair —
                        // its PreferenceKey width-matching could get stuck at
                        // a stale/tiny value under this screen's multi-stage
                        // sheet-restoration timing (relock -> re-present),
                        // which is what produced the "Verify and enable"
                        // button rendering as a tall sliver with its text
                        // wrapped one character per line. A fixed
                        // .frame(maxWidth: .infinity) on each label can't get
                        // stuck, since nothing is measured or fed back in.
                        VStack(spacing: 10) {
                            Button {
                                Task {
                                    if await viewModel.verify2FA(code: code) {
                                        Haptic.success()
                                        postedLabel = "Two-factor enabled"
                                        sessionStore.pendingTwoFactorSetupEmail = nil
                                    }
                                }
                            } label: {
                                Group {
                                    if viewModel.is2FABusy {
                                        CavnarShimmerText(text: "Verifying…", color: Color.cavnarInk)
                                    } else {
                                        Text("Verify and enable")
                                    }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.is2FABusy || code.count != 6))
                            .disabled(viewModel.is2FABusy || code.count != 6)

                            Button {
                                sessionStore.pendingTwoFactorSetupEmail = nil
                                dismiss()
                            } label: {
                                Text("Cancel").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                        .padding(.top, 6)
                    } else {
                        Text(selectedMethod == "sms"
                             ? "We'll text a 6-digit code to the phone number on file to confirm two-factor sign-in works before turning it on."
                             : "We'll email a 6-digit code to the address on file to confirm two-factor sign-in works before turning it on.")
                            .font(.cavnarBody(16))
                            .foregroundStyle(Color.cavnarInk3)

                        if hasPhone {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("SEND CODE BY")
                                    .font(.cavnarBody(12, weight: 700))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .tracking(0.6)
                                CavnarSegmentedControl(selection: $selectedMethod, options: ["email", "sms"]) { option in
                                    option == "sms" ? "Text" : "Email"
                                }
                            }
                            .padding(.top, 4)
                        }

                        if let error = viewModel.twoFAError {
                            Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                        }

                        // Plain full-width button — see the identical note
                        // on the "Verify and enable" button above for why
                        // CavnarFormButtonPair's width-matching was dropped
                        // from this screen specifically.
                        VStack(spacing: 10) {
                            Button {
                                Task {
                                    await viewModel.send2FATest(method: selectedMethod)
                                    // Recorded on SessionStore (survives a
                                    // Face ID relock) the instant a real
                                    // code goes out — see its doc comment.
                                    if let masked = viewModel.twoFATestMasked {
                                        sessionStore.pendingTwoFactorSetupEmail = masked
                                        sessionStore.pendingTwoFactorSetupMethod = viewModel.twoFATestMethod
                                    }
                                }
                            } label: {
                                Group {
                                    if viewModel.is2FABusy {
                                        CavnarShimmerText(text: "Sending…", color: Color.cavnarInk)
                                    } else {
                                        Text("Send test code")
                                    }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.is2FABusy))
                            .disabled(viewModel.is2FABusy)

                            Button {
                                dismiss()
                            } label: {
                                Text("Cancel").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                        .padding(.top, 6)
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Enable 2FA")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Enable 2FA") }
            // Single field, nothing to step between — checkmark only,
            // matching Apple's own single-field bar rather than showing
            // two permanently-disabled chevrons for show.
            .keyboardDoneToolbar { isCodeFocused = false }
            // Only the verify-and-enable success gets the posted moment —
            // sending the test code is a step along the way, not the
            // milestone itself.
            .cavnarPostedOverlay(postedLabel) { dismiss() }
            // A relock recreates AccountViewModel from scratch — this
            // restores its twoFATestMasked from SessionStore so the view
            // renders the "enter code" branch immediately instead of
            // resetting to "send test code" and losing the masked email.
            .onAppear {
                if viewModel.twoFATestMasked == nil, let pending = sessionStore.pendingTwoFactorSetupEmail {
                    viewModel.twoFATestMasked = pending
                    viewModel.twoFATestMethod = sessionStore.pendingTwoFactorSetupMethod
                    selectedMethod = sessionStore.pendingTwoFactorSetupMethod
                }
            }
        }
    }
}
