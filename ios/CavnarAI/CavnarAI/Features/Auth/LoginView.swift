import SwiftUI

private enum LoginField_: Hashable, CaseIterable {
    case username, password
}

/// The sign-in screen, rebuilt to the approved render: the wordmark
/// (no seal) over a living ember aurora + constellation, two glass fields
/// with SF Symbols and a focus-lit underline, "Forgot password?" right-
/// aligned at a full 44pt, a full-width ember Sign In with a slow sheen,
/// "or continue with", Apple and Google as white pills, and "Don't have an
/// account? Sign up" anchoring the bottom. The whole block is centered in
/// the screen (and still scrolls when the keyboard needs the room). Every
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

                // GeometryReader + minHeight is what centers the block:
                // shorter than the screen, it floats to the middle; taller
                // (keyboard up), it scrolls normally.
                GeometryReader { geo in
                    ScrollView {
                        VStack(spacing: 0) {
                            Spacer(minLength: 0)

                            brand
                                .padding(.bottom, LoginMetrics.spaceXXL)

                            form

                            divider
                                .padding(.top, LoginMetrics.spaceXL)
                                .padding(.bottom, LoginMetrics.spaceL)

                            social

                            anchor
                                .padding(.top, LoginMetrics.spaceXL)

                            Spacer(minLength: 0)
                        }
                        .padding(.horizontal, LoginMetrics.pageInset)
                        .padding(.vertical, LoginMetrics.spaceXL)
                        .frame(minHeight: geo.size.height)
                    }
                    .scrollDismissesKeyboard(.interactively)
                }
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
            // showed both. Same entrance LockedView uses. The glow is a
            // compositingGroup'd shadow at a modest radius — the first
            // pass shadowed the live vector wordmark at 30pt on every
            // frame of its own draw-in, which was part of the scroll lag.
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
            .compositingGroup()
            .shadow(color: Color.cavnarEmber.opacity(0.22), radius: 16)

            Text("Sign in to your restaurant")
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
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
                submitLabel: .go, onSubmit: { focusedField = nil; Task { await viewModel.submit() } },
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
                // Explicit, in the action — not only the style's isPressed
                // haptic. After Password AutoFill, the SecureField's binding
                // catches up when the field resigns, which is the same tap
                // that hits this button: at touch-down canSubmit is still
                // false, so the style's `new && !isDisabled` gate skips the
                // buzz, then the field commits, the button re-enables, and
                // touch-up still fires the action. The action haptic covers
                // that path; the style's covers every ordinary tap.
                Haptic.medium()
                // Drop the keyboard NOW. Left up, it stays through the swap
                // to the dashboard, so Home mounts with a keyboard-sized
                // bottom inset: the Ask Cavnar FAB (an overlay that honours
                // the keyboard safe area) lands high, then falls into place
                // as the keyboard animates away — the "positioned weird
                // before settling" glitch, plus a layout pass fighting
                // Home's own fade-in.
                focusedField = nil
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
                    .foregroundStyle(Color.cavnarInk)
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
