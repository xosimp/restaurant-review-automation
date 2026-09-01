import SwiftUI

private enum LoginField: Hashable, CaseIterable {
    case username, password
}

struct LoginView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel: LoginViewModel
    @FocusState private var focusedField: LoginField?

    init(sessionStore: SessionStore) {
        _viewModel = State(initialValue: LoginViewModel(sessionStore: sessionStore))
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Color.cavnarPaper.ignoresSafeArea()
                VStack(spacing: 28) {
                    Spacer()

                    VStack(spacing: 14) {
                        // The seal draws itself while the letters stamp in
                        // beside it — the one-time entrance for the first
                        // screen a session ever sees (see CavnarMotion).
                        CavnarLockupIntro(width: 220)
                        Text("Sign in to your restaurant")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                    }

                    VStack(spacing: 12) {
                        TextField("Username", text: $viewModel.username)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .cavnarTextFieldStyle()
                            .focused($focusedField, equals: .username)
                        SecureField("Password", text: $viewModel.password)
                            .cavnarTextFieldStyle()
                            .focused($focusedField, equals: .password)
                    }

                    if let error = viewModel.errorMessage {
                        Text(error)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarRed)
                            .multilineTextAlignment(.center)
                    }

                    Button {
                        Task { await viewModel.submit() }
                    } label: {
                        if viewModel.isLoading {
                            ProgressView().tint(.white)
                        } else {
                            Text("Sign In")
                        }
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
                    .disabled(!viewModel.canSubmit)

                    orDivider

                    VStack(spacing: 10) {
                        GoogleSignInButton(isLoading: viewModel.isLoading) {
                            Task { await viewModel.signInWithGoogle() }
                        }
                        AppleSignInButton(isLoading: viewModel.isLoading) {
                            Task { await viewModel.signInWithApple() }
                        }
                    }

                    Spacer()
                    Spacer()
                }
                .padding(28)
            }
            .keyboardNavToolbar($focusedField)
            .navigationDestination(
                isPresented: Binding(
                    get: { viewModel.twoFactorPendingToken != nil },
                    set: { isPresented in
                        if !isPresented { viewModel.twoFactorPendingToken = nil }
                    }
                )
            ) {
                if let pendingToken = viewModel.twoFactorPendingToken {
                    TwoFactorView(
                        viewModel: TwoFactorViewModel(
                            sessionStore: sessionStore,
                            pendingToken: pendingToken,
                            maskedEmail: viewModel.twoFactorMaskedEmail ?? ""
                        )
                    )
                }
            }
        }
    }

    private var orDivider: some View {
        HStack(spacing: 12) {
            Rectangle().fill(Color.cavnarPaper3).frame(height: 1)
            Text("or")
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk3)
            Rectangle().fill(Color.cavnarPaper3).frame(height: 1)
        }
    }
}

/// Google's brand guidelines call for their own logo mark on a plain white
/// pill regardless of the host app's theme — matches the treatment the web
/// login page already uses for the same button.
private struct GoogleSignInButton: View {
    var isLoading: Bool
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Text("G")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber)
                Text("Sign in with Google")
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(.black)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .background(.white)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        }
        .disabled(isLoading)
        .opacity(isLoading ? 0.6 : 1)
    }
}

private struct AppleSignInButton: View {
    var isLoading: Bool
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: "apple.logo")
                    .font(.system(size: 15, weight: .semibold))
                Text("Sign in with Apple")
                    .font(.cavnarBody(14, weight: 600))
            }
            .foregroundStyle(Color.cavnarInk)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(Color.cavnarPaper3, lineWidth: 1)
            )
        }
        .disabled(isLoading)
        .opacity(isLoading ? 0.6 : 1)
    }
}
