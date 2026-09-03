import SwiftUI

private enum ForgotField: Hashable, CaseIterable {
    case email, newPassword, confirm
}

/// "Forgot password?" from the sign-in screen — the whole reset stays in
/// the app now. Three steps on one sheet: the email, then the 6-digit
/// code that email delivers (same CavnarCodeEntry cells 2FA uses) with
/// the new password beneath it, then a posted-check and back to Sign In.
/// The server answers the first step identically whether or not the
/// email exists, so nothing here can be used to enumerate accounts.
struct ForgotPasswordSheet: View {
    let sessionStore: SessionStore
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focusedField: ForgotField?
    @FocusState private var codeFocused: Bool

    private enum Step { case email, code }
    @State private var step: Step = .email
    @State private var email: String
    @State private var code = ""
    @State private var newPassword = ""
    @State private var confirm = ""
    @State private var isWorking = false
    @State private var errorMessage: String?
    @State private var errorShake = 0
    @State private var postedLabel: String?

    init(sessionStore: SessionStore, prefill: String = "") {
        self.sessionStore = sessionStore
        _email = State(initialValue: prefill.contains("@") ? prefill : "")
    }

    private var canSend: Bool { email.contains("@") && !isWorking }
    private var canReset: Bool {
        code.count == 6 && newPassword.count >= 8 && confirm == newPassword && !isWorking
    }

    var body: some View {
        NavigationStack {
            ZStack {
                LoginBackground()
                ScrollView {
                    VStack(alignment: .leading, spacing: LoginMetrics.spaceL) {
                        switch step {
                        case .email: emailStep
                        case .code: codeStep
                        }
                    }
                    .padding(.horizontal, LoginMetrics.pageInset)
                    .padding(.top, LoginMetrics.spaceL)
                    .padding(.bottom, LoginMetrics.spaceXL)
                    .animation(.easeOut(duration: 0.25), value: errorMessage != nil)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .navigationTitle("Reset password")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Reset password") }
            .toolbarBackground(.hidden, for: .navigationBar)
            .keyboardNavToolbar($focusedField)
            .cavnarPostedOverlay(postedLabel) { dismiss() }
            .task { focusedField = .email }
        }
    }

    // MARK: - Step 1: email

    private var emailStep: some View {
        Group {
            Text("Enter the email on your account and we'll send a 6-digit code to reset your password.")
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)

            LoginField(
                systemImage: "envelope.fill", placeholder: "Email",
                text: $email, keyboardType: .emailAddress, textContentType: .emailAddress,
                submitLabel: .send, onSubmit: { Task { await sendCode() } },
                focus: $focusedField, field: .email,
                isError: errorMessage != nil, shakeTrigger: errorShake
            )

            if let errorMessage {
                LoginErrorBar(message: errorMessage)
            }

            primaryButton(title: "Send code", busy: "Sending…", enabled: canSend) {
                Task { await sendCode() }
            }

            backToSignIn
        }
    }

    // MARK: - Step 2: code + new password

    private var codeStep: some View {
        Group {
            Text("Code sent to \(email)")
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)

            CavnarCodeEntry(
                code: $code,
                isVerifying: isWorking,
                isError: errorMessage != nil,
                focus: $codeFocused
            )
            .onChange(of: code) { _, new in
                if new.count < 6 { errorMessage = nil }
                if new.count == 6 { focusedField = .newPassword }
            }
            .onAppear { codeFocused = true }

            LoginField(
                systemImage: "lock.fill", placeholder: "New password (8+ characters)",
                text: $newPassword, isSecure: true, textContentType: .newPassword,
                submitLabel: .next, onSubmit: { focusedField = .confirm },
                focus: $focusedField, field: .newPassword,
                isError: errorMessage != nil, shakeTrigger: errorShake
            )
            LoginField(
                systemImage: "lock.rotation", placeholder: "Confirm new password",
                text: $confirm, isSecure: true, textContentType: .newPassword,
                submitLabel: .go, onSubmit: { Task { await reset() } },
                focus: $focusedField, field: .confirm,
                isError: errorMessage != nil || (!confirm.isEmpty && confirm != newPassword), shakeTrigger: errorShake
            )

            if !confirm.isEmpty, confirm != newPassword {
                LoginErrorBar(message: "Those passwords don't match.")
            } else if let errorMessage {
                LoginErrorBar(message: errorMessage)
            }

            primaryButton(title: "Reset password", busy: "Resetting…", enabled: canReset) {
                Task { await reset() }
            }

            Button {
                Haptic.light()
                Task { await sendCode(resend: true) }
            } label: {
                Text("Didn't get it? Send again")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(maxWidth: .infinity, minHeight: LoginMetrics.touch)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(isWorking)

            backToSignIn
        }
        .transition(.opacity.combined(with: .move(edge: .trailing)))
    }

    // MARK: - Shared pieces

    private func primaryButton(title: String, busy: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Group {
                if isWorking {
                    CavnarShimmerText(text: busy)
                } else {
                    Text(title)
                }
            }
            .frame(maxWidth: .infinity)
            .frame(height: LoginMetrics.buttonHeight - 28)
        }
        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !enabled))
        .disabled(!enabled)
        .padding(.top, LoginMetrics.spaceS)
    }

    private var backToSignIn: some View {
        Button {
            Haptic.light()
            dismiss()
        } label: {
            Text("Back to sign in")
                .font(.cavnarBody(14, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
                .frame(maxWidth: .infinity, minHeight: LoginMetrics.touch)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: - Actions

    private func sendCode(resend: Bool = false) async {
        guard resend || canSend else { return }
        isWorking = true
        errorMessage = nil
        defer { isWorking = false }
        do {
            try await sessionStore.requestPasswordReset(email: email.trimmingCharacters(in: .whitespaces))
            Haptic.success()
            focusedField = nil
            code = ""
            withAnimation(.easeOut(duration: 0.3)) { step = .code }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            errorShake += 1
            Haptic.error()
        } catch {
            errorMessage = "Couldn't send the code. Try again."
            errorShake += 1
            Haptic.error()
        }
    }

    private func reset() async {
        guard canReset else { return }
        isWorking = true
        errorMessage = nil
        defer { isWorking = false }
        do {
            try await sessionStore.resetPassword(
                email: email.trimmingCharacters(in: .whitespaces),
                code: code,
                newPassword: newPassword
            )
            Haptic.success()
            focusedField = nil
            codeFocused = false
            postedLabel = "Password reset"
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            errorShake += 1
            Haptic.error()
        } catch {
            errorMessage = "Couldn't reset your password. Try again."
            errorShake += 1
            Haptic.error()
        }
    }
}
