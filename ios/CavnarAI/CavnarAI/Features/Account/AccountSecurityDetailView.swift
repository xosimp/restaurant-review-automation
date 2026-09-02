import SwiftUI

/// Opened from Account's "Security & devices" row. Option A ("identity
/// card") from the account-sheet design review: the sheet opens on the
/// answer to "am I protected?" — a shield tile, a one-word verdict, and
/// three status tiles (password, two-factor, sign-in alerts) — before any
/// setting. Then one Sign-in card and one Devices card.
struct AccountSecurityDetailView: View {
    let viewModel: AccountViewModel
    let account: AccountInfo
    @State private var showingChangePassword = false
    @State private var showing2FASetup = false
    @State private var disabledLabel: String?
    @Environment(SessionStore.self) private var sessionStore

    // Prefer the live summary (it refreshes after enable/disable 2FA and
    // the notify toggle) over the snapshot the sheet was opened with.
    private var live: AccountInfo { viewModel.summary?.account ?? account }
    private var twoFAByText: Bool { live.twoFAMethod == "sms" }

    private var passwordStrengthLabel: String {
        switch live.passwordStrength {
        case "strong": return "Strong"
        case "good": return "Good"
        case "weak": return "Weak"
        default: return "Set"
        }
    }
    private var passwordStrengthTone: Color {
        switch live.passwordStrength {
        case "strong": return .cavnarGreen
        case "weak": return .cavnarAmber
        default: return .cavnarInk
        }
    }

    var body: some View {
        NavigationStack {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                hero
                statusStrip
                signInSection
                devicesSection
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .accountSheetChrome("Security")
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

    // MARK: - Identity

    private var hero: some View {
        AccountHero(title: live.twoFAEnabled ? "Protected" : "Protect your account") {
            GlowBadge(systemImage: "checkmark.shield", size: 64)
        } subtitle: {
            Text("Signed in as ") + Text(live.username).font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarInk2)
        }
    }

    private var statusStrip: some View {
        HStack(spacing: 8) {
            AccountStatTile(
                label: "Password", value: passwordStrengthLabel,
                tone: passwordStrengthTone,
                detail: live.passwordChangedAt == nil ? "Never changed" : "Changed " + AccountRelativeTime.describe(live.passwordChangedAt).lowercased()
            )
            AccountStatTile(
                label: "2FA", value: live.twoFAEnabled ? "On" : "Off",
                tone: live.twoFAEnabled ? .cavnarGreen : .cavnarInk3,
                detail: live.twoFAEnabled ? "\(twoFAByText ? "Text" : "Email") · \(live.twoFAContactMasked ?? "")" : "Not set up",
                detailIsNumber: live.twoFAEnabled && twoFAByText
            )
            AccountStatTile(
                label: "Alerts", value: live.loginNotify ? "On" : "Off",
                tone: live.loginNotify ? .cavnarGreen : .cavnarInk3,
                detail: "New sign-ins"
            )
        }
    }

    // MARK: - Sign-in

    private var signInSection: some View {
        AccountSection(kicker: "Sign-in") {
            AccountKVRow(label: "Password") {
                AccountLink(title: "Change") { showingChangePassword = true }
            }
            if live.twoFAEnabled {
                AccountKVRow(label: "Two-factor code by") {
                    AccountValue(text: twoFAByText ? "Text message" : "Email")
                }
                AccountKVRow(label: "Two-factor") {
                    AccountLink(title: "Turn off", tone: .cavnarRed) {
                        Task {
                            if await viewModel.disable2FA() {
                                Haptic.success()
                                disabledLabel = "Two-factor disabled"
                            }
                        }
                    }
                }
            } else {
                AccountKVRow(label: "Two-factor") {
                    AccountLink(title: "Turn on") { showing2FASetup = true }
                }
            }
            AccountKVRow(label: "Sign-in notifications", showsDivider: false) {
                // No manual Haptic.selection() — Toggle/UISwitch already
                // fires its own automatic system haptic.
                Toggle("", isOn: Binding(
                    get: { live.loginNotify },
                    set: { newValue in Task { await viewModel.toggleLoginNotify(newValue) } }
                ))
                .labelsHidden()
                .tint(Color.cavnarEmber)
            }
        }
    }

    // MARK: - Devices

    private var devicesSection: some View {
        AccountSection(kicker: viewModel.sessions.isEmpty ? "Devices" : "Devices · \(viewModel.sessions.count)") {
            ForEach(Array(viewModel.sessions.enumerated()), id: \.element.id) { index, session in
                AccountDeviceRow(session: session, showsDivider: index < viewModel.sessions.count - 1)
            }
            Button {
                Task { await viewModel.revokeOtherSessions() }
            } label: {
                Text("Sign out all other devices").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .padding(.top, 12)
        }
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
