import SwiftUI

private enum LoginField: Hashable, CaseIterable {
    case username, password
}

struct LoginView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel: LoginViewModel
    @FocusState private var focusedField: LoginField?
    // False while RootView's launch splash still covers this on a cold
    // launch — the lockup isn't mounted until it lifts, so its draw-in
    // doesn't play hidden underneath.
    private let introReady: Bool
    // Cold launch (fresh install): the wordmark is traced and filled in by
    // the ember; after a sign-out in the same session it stamps in instead.
    private let coldLaunch: Bool

    init(sessionStore: SessionStore, introReady: Bool = true, coldLaunch: Bool = false) {
        _viewModel = State(initialValue: LoginViewModel(sessionStore: sessionStore))
        self.introReady = introReady
        self.coldLaunch = coldLaunch
    }

    var body: some View {
        NavigationStack {
            ZStack {
                VStack(spacing: 28) {
                    Spacer()

                    VStack(spacing: 14) {
                        // Wordmark only — no seal beside it. Same call
                        // already made everywhere else a lockup showed
                        // both together (social headers, email headers,
                        // the Face ID lock screen): it reads as the same
                        // mark shown twice, not two different things.
                        // Mirrors LockedView's own wordmark-only entrance
                        // exactly (RootView.swift) rather than
                        // CavnarLockupIntro, which still draws the seal.
                        if introReady {
                            Group {
                                if coldLaunch {
                                    CavnarWordmarkTraceIn(width: 260, aiTagOverhangs: true)
                                } else {
                                    CavnarWordmarkStampIn(width: 260, aiTagOverhangs: true)
                                }
                            }
                            .frame(height: 260 * (CavnarWordmarkLetterShape.boxHeight / CavnarWordmarkLetterShape.boxWidth))
                        } else {
                            Color.clear.frame(width: 260, height: 260 * (CavnarWordmarkLetterShape.boxHeight / CavnarWordmarkLetterShape.boxWidth))
                        }
                        Text("Sign in to your restaurant")
                            .font(.cavnarBody(15))
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
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarRed)
                            .multilineTextAlignment(.center)
                    }

                    Button {
                        Task { await viewModel.submit() }
                    } label: {
                        Group {
                            if viewModel.isLoading {
                                CavnarShimmerText(text: "Signing in…")
                            } else {
                                Text("Sign In")
                            }
                        }
                        // Was hugging its own text width while Google/Apple
                        // below it stretched full width — the one button
                        // on this screen that didn't match its siblings.
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
                    .disabled(!viewModel.canSubmit)

                    orDivider

                    VStack(spacing: 10) {
                        GoogleSignInButton(isLoading: viewModel.isLoading) {
                            Haptic.light()
                            Task { await viewModel.signInWithGoogle() }
                        }
                        AppleSignInButton(isLoading: viewModel.isLoading) {
                            Haptic.light()
                            Task { await viewModel.signInWithApple() }
                        }
                    }

                    Spacer()
                    Spacer()
                }
                .padding(28)
            }
            .cavnarModuleBackground()
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
