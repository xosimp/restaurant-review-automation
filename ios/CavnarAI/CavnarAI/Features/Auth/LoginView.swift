import SwiftUI

private enum LoginField_: Hashable, CaseIterable {
    case username, password
}

/// The sign-in screen, rebuilt to the approved render: the wordmark
/// (no seal) over a living ember aurora + constellation, a greeting, two
/// glass fields with SF Symbols and a focus-lit underline, "Forgot
/// password?" right-aligned at a full 44pt, a full-width ember Sign In
/// with a slow sheen, "or continue with", Apple and Google as white pills,
/// and "Don't have an account? Sign up" anchoring the bottom. Every
/// element rises in on a stagger; every button fires a haptic; every
/// failure shows the red bar, shakes the fields, and buzzes the error
/// pattern. All sizes come from LoginMetrics, colors from the palette.
struct LoginView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel: LoginViewModel
    @FocusState private var focusedField: LoginField_?
    @State private var showingForgot = false
    @State private var showingRegister = false
    // False while RootView's launch splash still covers this on a cold
    // launch — the wordmark isn't mounted until it lifts, so its draw-in
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

    // The entrance choreography, in seconds after the wordmark starts.
    private enum Cue {
        static let greeting = 0.12
        static let subtitle = 0.18
        static let field1 = 0.28
        static let field2 = 0.36
        static let primary = 0.44
        static let divider = 0.52
        static let apple = 0.60
        static let google = 0.68
        static let anchor = 0.80
    }

    private var wordmarkHeight: CGFloat {
        LoginMetrics.wordmarkWidth * (CavnarWordmarkLetterShape.boxHeight / CavnarWordmarkLetterShape.boxWidth)
    }

    var body: some View {
        NavigationStack {
            ZStack {
                LoginBackground()

                ScrollView {
                    VStack(spacing: 0) {
                        brand
                            .padding(.top, LoginMetrics.spaceHero)
                            .padding(.bottom, LoginMetrics.spaceXXL)

                        form

                        divider
                            .padding(.top, LoginMetrics.spaceXL)
                            .padding(.bottom, LoginMetrics.spaceL)

                        social

                        anchor
                            .padding(.top, LoginMetrics.spaceXL)
                    }
                    .padding(.horizontal, LoginMetrics.pageInset)
                    .padding(.bottom, LoginMetrics.spaceXL)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .keyboardNavToolbar($focusedField)
            .sheet(isPresented: $showingForgot) {
                ForgotPasswordSheet(sessionStore: sessionStore, prefill: viewModel.username)
            }
            .sheet(isPresented: $showingRegister) {
                RegisterView(sessionStore: sessionStore)
            }
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

    // MARK: - Brand

    private var brand: some View {
        VStack(spacing: LoginMetrics.spaceM) {
            // Wordmark only — the seal beside it was the same "two marks
            // side by side" call already made everywhere else a lockup
            // showed both. Same entrance LockedView uses.
            Group {
                if introReady {
                    if coldLaunch {
                        CavnarWordmarkTraceIn(width: LoginMetrics.wordmarkWidth, aiTagOverhangs: true)
                    } else {
                        CavnarWordmarkStampIn(width: LoginMetrics.wordmarkWidth, aiTagOverhangs: true)
                    }
                } else {
                    Color.clear
                }
            }
            .frame(width: LoginMetrics.wordmarkWidth, height: wordmarkHeight)
            .shadow(color: Color.cavnarEmber.opacity(0.25), radius: 30)

            Text("Welcome back")
                .font(.cavnarHeadline(24))
                .foregroundStyle(Color.cavnarInk)
                .loginRise(Cue.greeting, enabled: introReady)

            Text("Sign in to your restaurant")
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
                .padding(.top, -LoginMetrics.spaceXS)
                .loginRise(Cue.subtitle, enabled: introReady)
        }
    }

    // MARK: - Form

    private var form: some View {
        VStack(spacing: LoginMetrics.spaceM) {
            LoginField(
                systemImage: "envelope.fill", placeholder: "Email or username",
                text: $viewModel.username, keyboardType: .emailAddress, textContentType: .username,
                submitLabel: .next, onSubmit: { focusedField = .password },
                focus: $focusedField, field: .username,
                isError: viewModel.errorMessage != nil, shakeTrigger: viewModel.errorShake
            )
            .loginRise(Cue.field1, enabled: introReady)

            LoginField(
                systemImage: "lock.fill", placeholder: "Password",
                text: $viewModel.password, isSecure: true, textContentType: .password,
                submitLabel: .go, onSubmit: { Task { await viewModel.submit() } },
                focus: $focusedField, field: .password,
                isError: viewModel.errorMessage != nil, shakeTrigger: viewModel.errorShake
            )
            .loginRise(Cue.field2, enabled: introReady)

            if let error = viewModel.errorMessage {
                LoginErrorBar(message: error)
            }

            HStack {
                Spacer()
                Button {
                    Haptic.light()
                    showingForgot = true
                } label: {
                    Text("Forgot password?")
                        .font(.cavnarBody(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: LoginMetrics.touch)
                        .padding(.horizontal, LoginMetrics.spaceXS)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            .padding(.top, -LoginMetrics.spaceS)
            .loginRise(Cue.field2, enabled: introReady)

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
                .frame(maxWidth: .infinity)
                .frame(height: LoginMetrics.buttonHeight - 28)   // the style adds 14pt top + bottom
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !viewModel.canSubmit))
            .disabled(!viewModel.canSubmit)
            .loginSheen()
            .padding(.top, -LoginMetrics.spaceXS)
            .loginRise(Cue.primary, enabled: introReady)
        }
        .animation(.easeOut(duration: 0.25), value: viewModel.errorMessage != nil)
    }

    // MARK: - Divider + social

    private var divider: some View {
        HStack(spacing: LoginMetrics.spaceM) {
            LinearGradient(colors: [.clear, Color.cavnarInk3.opacity(0.45), .clear], startPoint: .leading, endPoint: .trailing)
                .frame(height: 1)
            Text("or continue with")
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize()
            LinearGradient(colors: [.clear, Color.cavnarInk3.opacity(0.45), .clear], startPoint: .leading, endPoint: .trailing)
                .frame(height: 1)
        }
        .loginRise(Cue.divider, enabled: introReady)
    }

    private var social: some View {
        VStack(spacing: LoginMetrics.spaceM) {
            LoginSocialButton(title: "Continue with Apple", isLoading: viewModel.isLoading) {
                Image(systemName: "apple.logo")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(.black)
            } action: {
                Task { await viewModel.signInWithApple() }
            }
            .loginRise(Cue.apple, enabled: introReady)

            LoginSocialButton(title: "Continue with Google", isLoading: viewModel.isLoading) {
                // Google's real 4-color G — the same bundled mark the
                // Connections screen uses.
                Image("GoogleMark").resizable().aspectRatio(contentMode: .fit)
            } action: {
                Task { await viewModel.signInWithGoogle() }
            }
            .loginRise(Cue.google, enabled: introReady)
        }
    }

    // MARK: - Anchor

    private var anchor: some View {
        HStack(spacing: LoginMetrics.spaceXS) {
            Text("Don't have an account?")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
            Button {
                Haptic.light()
                showingRegister = true
            } label: {
                Text("Sign up")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(minHeight: LoginMetrics.touch)
                    .padding(.horizontal, LoginMetrics.spaceXS)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .frame(maxWidth: .infinity)
        .loginRise(Cue.anchor, enabled: introReady)
    }
}
