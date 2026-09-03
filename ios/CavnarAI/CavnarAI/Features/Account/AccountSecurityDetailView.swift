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
    @State private var showingSignInHistory = false
    @State private var showingBackupCodes = false
    @State private var passcodeSheet: AppPasscodeSheet.Mode?
    @State private var disabledLabel: String?
    // The same posted-check overlay 2FA-disable already used, now shared
    // with Sign-in notifications — .removed (red, a drawn bar) for turning
    // something off, .success (ember, the checkmark) for everything else.
    @State private var disabledTone: CavnarPostedTone = .success
    @State private var isRevoking = false
    @Environment(SessionStore.self) private var sessionStore

    // Prefer the live summary (it refreshes after enable/disable 2FA and
    // the notify toggle) over the snapshot the sheet was opened with.
    private var live: AccountInfo { viewModel.summary?.account ?? account }
    private var twoFAByText: Bool { live.twoFAMethod == "sms" }

    // "Set" read as filler ("of course it's set, you have an account") once
    // device feedback pointed it out — an account created before strength
    // tracking existed, and never changed since, has neither value at all.
    // "Unrated" says that honestly instead of a placeholder pretending to
    // be information. auth.py now scores strength at account CREATION too
    // (not just on a later change), so this only shows up for accounts
    // older than that.
    private var passwordStrengthLabel: String {
        switch live.passwordStrength {
        case "strong": return "Strong"
        case "good": return "Good"
        case "weak": return "Weak"
        default: return "Unrated"
        }
    }
    private var passwordStrengthTone: Color {
        switch live.passwordStrength {
        case "strong": return .cavnarGreen
        case "good": return .cavnarGreen
        case "weak": return .cavnarRed
        default: return .cavnarInk3
        }
    }

    var body: some View {
        NavigationStack {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                hero
                statusStrip
                signInSection
                deviceLockSection
                devicesSection
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .accountSheetChrome("Security")
        // Own load, not just a read of whatever AccountView's own .task
        // happened to populate — this sheet can open before that initial
        // fetch resolves (tap Account, tap Security & devices, fast),
        // which is exactly what made the current device "only show up
        // after clicking Sign out all other devices" — that button's own
        // success path happens to call loadSessions() again as a side
        // effect, so it looked like the click was what populated the
        // list, when really it was just the first fetch to actually land.
        .task { await viewModel.loadSessions() }
        .sheet(isPresented: $showingChangePassword) {
            ChangePasswordSheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showing2FASetup) {
            TwoFactorSetupSheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showingSignInHistory) {
            AccountSignInHistoryView(viewModel: viewModel)
        }
        .sheet(item: $passcodeSheet) { mode in
            AppPasscodeSheet(mode: mode)
        }
        .sheet(isPresented: $showingBackupCodes) {
            AccountBackupCodesView(viewModel: viewModel)
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
        .cavnarPostedOverlay(disabledLabel, tone: disabledTone) { disabledLabel = nil }
        }
    }

    // MARK: - Identity

    private var hero: some View {
        AccountHero(title: live.twoFAEnabled ? "Protected" : "Protect your account") {
            GlowBadge(systemImage: "checkmark.shield", size: 64)
        } subtitle: {
            Text("Signed in as ") + Text(live.username).font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarInk2)
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
            // Failures on these controls used to be completely silent, so a
            // failed "Turn off" left the user believing 2FA was off when it
            // was still on (audit 2.4).
            if let error = viewModel.securityActionError {
                Text(error)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarRed)
                    .padding(.bottom, 4)
            }
            AccountKVRow(label: "Password") {
                AccountLink(title: "Change") { showingChangePassword = true }
            }
            if live.twoFAEnabled {
                AccountKVRow(label: "2FA code by") {
                    AccountValue(text: twoFAByText ? "Text message" : "Email")
                }
                AccountKVRow(label: "Backup codes") {
                    AccountLink(title: "View") { showingBackupCodes = true }
                }
            }
            // Always an unconditional sibling — only its trailing link
            // branches. This row used to be the whole AccountKVRow wrapped
            // in the if/else above, and that was enough to make it
            // measurably shorter than Password/Sign-in notifications (both
            // unconditional siblings) despite using the identical
            // component: SwiftUI's _ConditionalContent wrapper around a
            // top-level if/else branch doesn't propagate a child's own
            // .frame(minHeight:) through to the parent VStack's layout
            // negotiation as reliably as an unconditional sibling does.
            // Confirmed by direct pixel measurement — this row rendered at
            // 51pt against its siblings' 64pt each, a real, visible gap
            // device feedback caught, not a padding value being wrong.
            AccountKVRow(label: "2FA") {
                if live.twoFAEnabled {
                    AccountLink(title: "Turn off", tone: .cavnarRed) {
                        Task {
                            if await viewModel.disable2FA() {
                                Haptic.success()
                                disabledLabel = "Two-factor disabled"
                            }
                        }
                    }
                } else {
                    AccountLink(title: "Turn on") { showing2FASetup = true }
                }
            }
            // A "Turn on"/"Turn off" link, not a Toggle — matches 2FA's
            // own trailing control right above it (a Toggle's native
            // ~31pt height sat taller than every Link row around it,
            // which is what made this row read as misaligned against its
            // siblings; every row in this card is now the exact same
            // AccountLink shape, so they can't drift apart again).
            AccountKVRow(label: "Sign-in activity") {
                AccountLink(title: "View") { showingSignInHistory = true }
            }
            AccountKVRow(label: "Sign-in notifications", showsDivider: false) {
                if live.loginNotify {
                    AccountLink(title: "Turn off", tone: .cavnarRed) {
                        Task {
                            if await viewModel.toggleLoginNotify(false) {
                                Haptic.success()
                                disabledTone = .removed
                                disabledLabel = "Sign-in notifications off"
                            }
                        }
                    }
                } else {
                    AccountLink(title: "Turn on") {
                        Task {
                            if await viewModel.toggleLoginNotify(true) {
                                Haptic.success()
                                disabledTone = .success
                                disabledLabel = "Sign-in notifications on"
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - This device

    // Device-local, not a network round trip — a plain Toggle, matching
    // AccountView's own appIconRow precedent, not the AccountLink
    // Turn-on/Turn-off pattern the network-backed settings above use.
    private var deviceLockSection: some View {
        AccountSection(kicker: "This device") {
            if sessionStore.biometricsUnavailable {
                Text("This device has no passcode or Face ID set up, so the app can't lock itself. Set a device passcode in Settings to turn this on.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.bottom, 4)
            }
            if !sessionStore.reentryProtected {
                Text("Nothing is protecting the app when you reopen it — turn Face ID back on or set an app passcode.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.bottom, 4)
            }
            AccountKVRow(label: "Require Face ID to reopen") {
                Toggle("", isOn: Binding(
                    get: { sessionStore.biometricLockEnabled },
                    set: { newValue in
                        Haptic.light()
                        sessionStore.biometricLockEnabled = newValue
                    }
                ))
                .labelsHidden()
                .tint(Color.cavnarEmber)
            }
            // The fallback gate for when Face ID is off — see AppPasscode.
            // Set/Change/Remove all confirm through the same passcode pad
            // the lock screen uses.
            AccountKVRow(label: "App passcode", showsDivider: false) {
                if sessionStore.appPasscodeSet {
                    HStack(spacing: 14) {
                        AccountLink(title: "Change") { passcodeSheet = .change }
                        AccountLink(title: "Remove", tone: .cavnarRed) { passcodeSheet = .remove }
                    }
                } else {
                    AccountLink(title: "Set") { passcodeSheet = .create }
                }
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
                Haptic.light()
                guard !isRevoking else { return }
                isRevoking = true
                Task {
                    let ok = await viewModel.revokeOtherSessions()
                    isRevoking = false
                    // Verified end-to-end (revoke_other_sessions deletes
                    // every session row for this user EXCEPT the current
                    // token — a live test against the real DB confirmed
                    // the current device survives and only the others go)
                    // — this was only ever missing its own confirmation,
                    // not actually broken. Only ever fires on the real
                    // success response.
                    if ok {
                        disabledTone = .success
                        disabledLabel = "Signed out of other devices"
                    }
                }
            } label: {
                // CavnarSecondaryButtonStyle's own padding/shape is
                // symmetric on every axis (verified against its source —
                // no asymmetric shadow or offset like the primary style
                // carries), so this stays defensively explicit rather
                // than relying on defaults: centered alignment named
                // outright, and multilineTextAlignment set in case this
                // label ever wraps on a narrower device.
                Group {
                    if isRevoking {
                        CavnarShimmerText(text: "Signing out…", color: Color.cavnarEmber)
                    } else {
                        Text("Sign out all other devices")
                    }
                }
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity, alignment: .center)
            }
            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: isRevoking))
            .disabled(isRevoking)
            // Matches every other button group's top gap in this sheet
            // (Change/Cancel, Turn on/Cancel) — was 12pt, the one button
            // in Account that didn't match the shared 6pt rhythm.
            .padding(.top, 6)
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
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    // Plain full-width buttons, not CavnarFormButtonPair —
                    // this sheet is reached through the same two-level
                    // sheet chain (AccountView -> AccountSecurityDetailView
                    // -> here) that first exposed the PreferenceKey width-
                    // matching bug on TwoFactorSetupSheet (see its own
                    // comment). Device feedback confirmed the identical
                    // narrow-button symptom here too, so this gets the
                    // same fix rather than waiting for a third report.
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                await viewModel.changePassword(current: current, newPassword: newPassword)
                                if viewModel.changePasswordSucceeded {
                                    Haptic.success()
                                    postedLabel = "Password changed"
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isChangingPassword {
                                    CavnarShimmerText(text: "Changing…", color: Color.cavnarInk)
                                } else {
                                    Text("Change password")
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
    // Shown once, right after the first successful verify — see
    // AccountViewModel.freshBackupCodes' doc comment.
    @State private var showingCodesStep = false
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
                    if showingCodesStep {
                        backupCodesStep
                    } else if let masked = viewModel.twoFATestMasked {
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
                                        handleVerified()
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
                                        handleVerified()
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
                                    .font(.cavnarBody(13, weight: 700))
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

    // Interposes a "save these codes" step between a successful verify and
    // the final posted-checkmark dismiss — codes are shown exactly once,
    // right here, since only their hash is ever persisted server-side.
    private func handleVerified() {
        Haptic.success()
        sessionStore.pendingTwoFactorSetupEmail = nil
        if viewModel.freshBackupCodes?.isEmpty == false {
            showingCodesStep = true
        } else {
            postedLabel = "Two-factor enabled"
        }
    }

    private var backupCodesStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Two-factor is on. Save these backup codes somewhere safe — each works once to sign in if you lose access to email or text.")
                .font(.cavnarBody(16))
                .foregroundStyle(Color.cavnarInk3)

            VStack(spacing: 0) {
                ForEach(Array((viewModel.freshBackupCodes ?? []).enumerated()), id: \.offset) { index, code in
                    HStack {
                        Text(code).font(.cavnarNumber(16, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Spacer()
                    }
                    .padding(.vertical, 10)
                    if index < (viewModel.freshBackupCodes?.count ?? 0) - 1 {
                        AccountRowDivider()
                    }
                }
            }
            .cavnarCard()

            Button {
                Haptic.light()
                postedLabel = "Two-factor enabled"
            } label: {
                Text("I've saved these codes").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
        }
    }
}
