import SwiftUI
import Observation

private enum RegisterField: Hashable, CaseIterable {
    case restaurant, name, email, username, password, phone
}

@Observable
@MainActor
final class RegisterViewModel {
    var restaurantName = ""
    var ownerName = ""
    var email = ""
    var username = ""
    var password = ""
    var phone = ""
    var isSubmitting = false
    var errorMessage: String? {
        didSet { if errorMessage != nil { errorShake += 1 } }
    }
    var errorShake = 0
    var succeeded = false

    private let sessionStore: SessionStore

    init(sessionStore: SessionStore) {
        self.sessionStore = sessionStore
    }

    var canSubmit: Bool {
        restaurantName.trimmingCharacters(in: .whitespaces).count >= 2
            && email.contains("@")
            && username.trimmingCharacters(in: .whitespaces).count >= 3
            && password.count >= 8
            && !isSubmitting
    }

    /// Same rules the server enforces, checked first so the common misses
    /// get a specific message without a round trip.
    private func localValidationError() -> String? {
        if restaurantName.trimmingCharacters(in: .whitespaces).count < 2 { return "Enter your restaurant's name." }
        if !email.contains("@") || !email.contains(".") { return "Enter a valid email address." }
        let u = username.trimmingCharacters(in: .whitespaces).lowercased()
        if u.count < 3 || u.count > 30 || u.rangeOfCharacter(from: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789._-").inverted) != nil {
            return "Username must be 3–30 characters — letters, numbers, dots, dashes, or underscores."
        }
        if password.count < 8 { return "Password must be at least 8 characters." }
        return nil
    }

    func submit() async {
        guard canSubmit else { return }
        if let local = localValidationError() {
            errorMessage = local
            Haptic.error()
            return
        }
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            try await sessionStore.register(SessionStore.RegisterBody(
                restaurantName: restaurantName.trimmingCharacters(in: .whitespaces),
                ownerName: ownerName.trimmingCharacters(in: .whitespaces),
                email: email.trimmingCharacters(in: .whitespaces).lowercased(),
                username: username.trimmingCharacters(in: .whitespaces).lowercased(),
                password: password,
                phone: phone.trimmingCharacters(in: .whitespaces)
            ))
            succeeded = true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            Haptic.error()
        } catch {
            errorMessage = "Couldn't create your account. Try again."
            Haptic.error()
        }
    }
}

/// "Don't have an account? Sign up" — the app's first self-serve
/// registration. Creates the restaurant + owner login on the trial tier
/// with every module on (the Restaurant dataclass's own defaults, same
/// as an admin-created client), and signs the new owner straight in:
/// /mobile/api/register returns the same {token, user} the login route
/// does, so once the posted-check plays the session flips and RootView
/// swaps this sheet out for Home on its own.
struct RegisterView: View {
    @State private var viewModel: RegisterViewModel
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focusedField: RegisterField?
    @State private var postedLabel: String?

    init(sessionStore: SessionStore) {
        _viewModel = State(initialValue: RegisterViewModel(sessionStore: sessionStore))
    }

    var body: some View {
        NavigationStack {
            ZStack {
                LoginBackground()
                ScrollView {
                    VStack(alignment: .leading, spacing: LoginMetrics.spaceM) {
                        VStack(alignment: .leading, spacing: LoginMetrics.spaceXS) {
                            Text("Create your account")
                                .font(.cavnarHeadline(24))
                                .foregroundStyle(Color.cavnarInk)
                            Text("Every module on, free to start. Your restaurant's on Cavnar AI in under a minute.")
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.bottom, LoginMetrics.spaceS)

                        sectionLabel("Restaurant")
                        LoginField(
                            systemImage: "building.2.fill", placeholder: "Restaurant name",
                            text: $viewModel.restaurantName, textContentType: .organizationName, autocapitalization: .words,
                            onSubmit: { focusedField = .name },
                            focus: $focusedField, field: .restaurant,
                            isError: viewModel.errorMessage != nil, shakeTrigger: viewModel.errorShake
                        )

                        sectionLabel("You")
                            .padding(.top, LoginMetrics.spaceS)
                        LoginField(
                            systemImage: "person.fill", placeholder: "Your name",
                            text: $viewModel.ownerName, textContentType: .name, autocapitalization: .words,
                            onSubmit: { focusedField = .email },
                            focus: $focusedField, field: .name,
                            isError: viewModel.errorMessage != nil, shakeTrigger: viewModel.errorShake
                        )
                        LoginField(
                            systemImage: "envelope.fill", placeholder: "Email",
                            text: $viewModel.email, keyboardType: .emailAddress, textContentType: .emailAddress,
                            onSubmit: { focusedField = .username },
                            focus: $focusedField, field: .email,
                            isError: viewModel.errorMessage != nil, shakeTrigger: viewModel.errorShake
                        )
                        LoginField(
                            systemImage: "phone.fill", placeholder: "Phone (optional)",
                            text: $viewModel.phone, keyboardType: .phonePad, textContentType: .telephoneNumber,
                            submitLabel: .done,
                            focus: $focusedField, field: .phone,
                            isError: false, shakeTrigger: 0
                        )

                        sectionLabel("Sign-in")
                            .padding(.top, LoginMetrics.spaceS)
                        LoginField(
                            systemImage: "at", placeholder: "Username",
                            text: $viewModel.username, textContentType: .username,
                            onSubmit: { focusedField = .password },
                            focus: $focusedField, field: .username,
                            isError: viewModel.errorMessage != nil, shakeTrigger: viewModel.errorShake
                        )
                        LoginField(
                            systemImage: "lock.fill", placeholder: "Password (8+ characters)",
                            text: $viewModel.password, isSecure: true, textContentType: .newPassword,
                            submitLabel: .go, onSubmit: { Task { await submit() } },
                            focus: $focusedField, field: .password,
                            isError: viewModel.errorMessage != nil, shakeTrigger: viewModel.errorShake
                        )

                        if let error = viewModel.errorMessage {
                            LoginErrorBar(message: error)
                        }

                        Button {
                            Task { await submit() }
                        } label: {
                            Group {
                                if viewModel.isSubmitting {
                                    CavnarShimmerText(text: "Creating your account…")
                                } else {
                                    Text("Create account")
                                }
                            }
                            .frame(maxWidth: .infinity)
                            .frame(height: LoginMetrics.buttonHeight - 28)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
                        .disabled(!viewModel.canSubmit)
                        .loginSheen()
                        .padding(.top, LoginMetrics.spaceS)

                        Text("By continuing you agree to Cavnar AI's terms. Will reviews every new account personally.")
                            .font(.cavnarBody(12.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .multilineTextAlignment(.center)
                            .frame(maxWidth: .infinity)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.top, LoginMetrics.spaceXS)

                        HStack(spacing: LoginMetrics.spaceXS) {
                            Text("Already have an account?")
                                .font(.cavnarBody(14))
                                .foregroundStyle(Color.cavnarInk3)
                            Button {
                                Haptic.light()
                                dismiss()
                            } label: {
                                Text("Sign in")
                                    .font(.cavnarBody(14, weight: 700))
                                    .foregroundStyle(Color.cavnarEmber2)
                                    .frame(minHeight: LoginMetrics.touch)
                                    .padding(.horizontal, LoginMetrics.spaceXS)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.top, LoginMetrics.spaceS)
                    }
                    .padding(.horizontal, LoginMetrics.pageInset)
                    .padding(.top, LoginMetrics.spaceL)
                    .padding(.bottom, LoginMetrics.spaceXL)
                    .animation(.easeOut(duration: 0.25), value: viewModel.errorMessage != nil)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .navigationTitle("Sign up")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar("Sign up") }
            .toolbarBackground(.hidden, for: .navigationBar)
            .keyboardNavToolbar($focusedField)
            // The session has already flipped by the time this plays — the
            // overlay's own finish just closes the sheet, and RootView is
            // already showing Home underneath.
            .cavnarPostedOverlay(postedLabel) { dismiss() }
            .task { focusedField = .restaurant }
        }
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(12, weight: 700))
            .tracking(1.2)
            .foregroundStyle(Color.cavnarInk3)
    }

    private func submit() async {
        focusedField = nil
        await viewModel.submit()
        if viewModel.succeeded {
            Haptic.success()
            postedLabel = "Welcome to Cavnar AI"
        }
    }
}
