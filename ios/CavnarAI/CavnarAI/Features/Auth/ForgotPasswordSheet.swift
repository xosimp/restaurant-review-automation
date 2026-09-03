import SwiftUI

private enum ForgotField: Hashable, CaseIterable {
    case email
}

/// "Forgot password?" from the sign-in screen. One field, one button;
/// on success it flips to a "check your email" state instead of closing,
/// since the next step happens in their inbox (the reset link opens the
/// web reset page — a one-time flow, fine outside the app). The server
/// always answers ok whether or not the email exists, so this state is
/// shown either way and can't be used to enumerate accounts.
struct ForgotPasswordSheet: View {
    let sessionStore: SessionStore
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focusedField: ForgotField?
    @State private var email: String
    @State private var isSending = false
    @State private var sent = false
    @State private var errorMessage: String?
    @State private var errorShake = 0

    init(sessionStore: SessionStore, prefill: String = "") {
        self.sessionStore = sessionStore
        _email = State(initialValue: prefill.contains("@") ? prefill : "")
    }

    private var canSubmit: Bool { email.contains("@") && !isSending }

    var body: some View {
        NavigationStack {
            ZStack {
                LoginBackground()
                ScrollView {
                    VStack(alignment: .leading, spacing: LoginMetrics.spaceL) {
                        if sent {
                            sentState
                        } else {
                            formState
                        }
                    }
                    .padding(.horizontal, LoginMetrics.pageInset)
                    .padding(.top, LoginMetrics.spaceL)
                    .padding(.bottom, LoginMetrics.spaceXL)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .navigationTitle("Reset password")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Reset password") }
            .toolbarBackground(.hidden, for: .navigationBar)
            .keyboardNavToolbar($focusedField)
            .task { focusedField = .email }
        }
    }

    private var formState: some View {
        Group {
            Text("Enter the email on your account and we'll send a link to choose a new password.")
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)

            LoginField(
                systemImage: "envelope.fill", placeholder: "Email",
                text: $email, keyboardType: .emailAddress, textContentType: .emailAddress,
                submitLabel: .send, onSubmit: { Task { await send() } },
                focus: $focusedField, field: .email,
                isError: errorMessage != nil, shakeTrigger: errorShake
            )

            if let errorMessage {
                LoginErrorBar(message: errorMessage)
            }

            Button {
                Task { await send() }
            } label: {
                Group {
                    if isSending {
                        CavnarShimmerText(text: "Sending…")
                    } else {
                        Text("Send reset link")
                    }
                }
                .frame(maxWidth: .infinity)
                .frame(height: LoginMetrics.buttonHeight - 28)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
            .disabled(!canSubmit)
            .padding(.top, LoginMetrics.spaceS)

            Button {
                Haptic.light()
                dismiss()
            } label: {
                Text("Back to sign in")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(maxWidth: .infinity, minHeight: LoginMetrics.touch)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .animation(.easeOut(duration: 0.25), value: errorMessage != nil)
    }

    private var sentState: some View {
        VStack(spacing: LoginMetrics.spaceL) {
            GlowBadge(systemImage: "envelope.open.fill", size: 64)
                .padding(.top, LoginMetrics.spaceXL)
            Text("Check your email")
                .font(.cavnarHeadline(24))
                .foregroundStyle(Color.cavnarInk)
            Text("If there's an account for \(email), a reset link is on its way. It expires in an hour.")
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                Haptic.light()
                dismiss()
            } label: {
                Text("Back to sign in")
                    .frame(maxWidth: .infinity)
                    .frame(height: LoginMetrics.buttonHeight - 28)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .padding(.top, LoginMetrics.spaceS)
        }
        .frame(maxWidth: .infinity)
        .transition(.opacity.combined(with: .move(edge: .bottom)))
    }

    private func send() async {
        guard canSubmit else { return }
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        do {
            try await sessionStore.requestPasswordReset(email: email.trimmingCharacters(in: .whitespaces))
            Haptic.success()
            focusedField = nil
            withAnimation(.easeOut(duration: 0.3)) { sent = true }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            errorShake += 1
        } catch {
            errorMessage = "Couldn't send the reset link. Try again."
            errorShake += 1
        }
    }
}
