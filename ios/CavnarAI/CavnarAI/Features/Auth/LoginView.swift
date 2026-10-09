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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var viewModel: LoginViewModel
    @FocusState private var focusedField: LoginField_?
    @State private var showingForgot = false
    @State private var showingStaffSignIn = false
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
                LoginBackground(paused: sessionStore.isAuthenticated)

                // GeometryReader + minHeight is what centers the block:
                // shorter than the screen, it floats to the middle; taller
                // (keyboard up), it scrolls normally.
                GeometryReader { geo in
                    ScrollView {
                        VStack(spacing: 0) {
                            // The Ember Core in the open space above the
                            // wordmark, centred between the top of the
                            // screen and the wordmark (10/8/26): this region
                            // and the Spacer at the bottom share what the
                            // block leaves, so the form stays centred.
                            ember
                                .frame(maxWidth: .infinity, maxHeight: .infinity)
                                .padding(.vertical, LoginMetrics.spaceL)

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
            // A passkey saved for dashboard.cavnar.ai is offered in the
            // keyboard's QuickType bar over the username field (parity #57).
            .task { viewModel.startPasskeyAutoFill() }
            .onDisappear { viewModel.stopPasskeyAutoFill() }
            .sheet(isPresented: $showingForgot) {
                ForgotPasswordSheet(sessionStore: sessionStore, prefill: viewModel.username)
            }
            .sheet(isPresented: $showingRegister) {
                RegisterView(sessionStore: sessionStore)
            }
            .fullScreenCover(isPresented: $showingStaffSignIn) {
                // Full screen rather than a sheet: this is the whole of the
                // employee product, not a detour inside the owner one.
                // Its own Close (44pt, labelled). Once a staff sign-in
                // lands, RootView roots this phone on the staff app — and on
                // the staff PIN pad from then on, not this form (H10).
                StaffLoginView(onClose: { showingStaffSignIn = false })
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
                            maskedEmail: viewModel.twoFactorMaskedEmail ?? "",
                            channel: viewModel.twoFactorChannel
                        )
                    )
                }
            }
        }
    }

    // MARK: - Brand

    /// Cavnar AI itself, met first (the Ember Core, 10/8/26) — the web's
    /// sign-in does the same. Mounted with the wordmark so it never runs
    /// hidden under the launch splash.
    private var ember: some View {
        Group {
            if introReady {
                EmberCoreView(size: 72)
                    .transition(reduceMotion ? .opacity : .opacity.combined(with: .scale(scale: 0.86)))
            } else {
                Color.clear.frame(width: 72, height: 72)
            }
        }
        .animation(.easeOut(duration: 0.6), value: introReady)
    }

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
                .font(.cavnar(.body))
                .foregroundStyle(Color.cavnarInk2)
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

            // A fixed slot, always present. The bar used to be inserted and
            // removed with the error, and because this whole block is
            // vertically centered, every appearance moved the wordmark up
            // and the Apple/Google buttons down — twice per tap, since the
            // attempt cleared the error before setting it again. The slot
            // holds the height; only the bar inside it fades and shakes.
            ZStack {
                if let error = viewModel.errorMessage {
                    LoginErrorBar(message: error, shakeTrigger: viewModel.errorShake)
                }
            }
            .frame(minHeight: LoginMetrics.errorBarHeight)
            .frame(maxWidth: .infinity)

            HStack {
                // Staff don't have a username or a password — they have a
                // name on a roster and four digits. Sending them down the
                // owner sign-in form and letting it fail is the single most
                // likely way this feature gets called broken, so the other
                // door is on the same screen.
                Button {
                    Haptic.light()
                    showingStaffSignIn = true
                } label: {
                    // Ink2, Label (iOS readability round): staff looking for
                    // their own door must be able to read it.
                    Text("I'm staff")
                        .font(.cavnar(.label))
                        .foregroundStyle(Color.cavnarInk2)
                        .frame(minHeight: LoginMetrics.touch)
                        .padding(.horizontal, LoginMetrics.spaceXS)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                Spacer()
                Button {
                    Haptic.light()
                    showingForgot = true
                } label: {
                    Text("Forgot password?")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
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
        .onChange(of: viewModel.username) { _, _ in viewModel.clearErrorOnEdit() }
        .onChange(of: viewModel.password) { _, _ in viewModel.clearErrorOnEdit() }
        // Password AutoFill fills both fields at once, which is the moment
        // canSubmit flips true — and it's also the moment the engine is
        // coldest, because the Face ID / AutoFill flow has just torn the
        // app's haptic connection down (see Haptic). Warming it here means
        // the engine is already spinning by the time the finger lands on
        // Sign In, which is the tap that had no buzz.
        .onChange(of: viewModel.canSubmit) { _, ready in
            if ready { Haptic.warmUp() }
        }
    }

    // MARK: - Divider + social

    private var divider: some View {
        HStack(spacing: LoginMetrics.spaceM) {
            LinearGradient(colors: [.clear, Color.cavnarInk3.opacity(0.45), .clear], startPoint: .leading, endPoint: .trailing)
                .frame(height: 1)
            Text("or continue with")
                .font(.cavnarBody(CavnarType.caption, weight: 600))
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

            // Face ID / Touch ID with a passkey added in Account → Security
            // (here or on the web) — the same credential either place.
            LoginSocialButton(title: "Sign in with a passkey", isLoading: viewModel.isLoading) {
                Image(systemName: "person.badge.key.fill")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(Color.cavnarInk)
            } action: {
                Task { await viewModel.signInWithPasskey() }
            }
            .loginRise(Cue.google, enabled: introReady)
        }
    }

    // MARK: - Anchor

    /// Self-serve signup is closed: every account is set up by Will directly,
    /// and the backend refuses /mobile/api/register with a 403 unless
    /// ALLOW_PUBLIC_SIGNUP is set (see mobile_api.public_signup_open). The
    /// button and RegisterView are kept, not deleted — flip this to true on
    /// the day the backend flag is turned on, and the flow works again.
    private let publicSignupOpen = false

    private var anchor: some View {
        HStack(spacing: LoginMetrics.spaceXS) {
            if publicSignupOpen {
                Text("Don't have an account?")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk2)
                Button {
                    Haptic.light()
                    showingRegister = true
                } label: {
                    Text("Sign up")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minHeight: LoginMetrics.touch)
                        .padding(.horizontal, LoginMetrics.spaceXS)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                // The address is a mailto: link, not text to copy by hand.
                (Text("Accounts are set up with you directly \u{2014} email ")
                    + Text("[will@cavnar.ai](mailto:will@cavnar.ai)").foregroundStyle(Color.cavnarEmber2))
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk2)
                    .tint(Color.cavnarEmber2)
                    .multilineTextAlignment(.center)
                    .frame(minHeight: LoginMetrics.touch)
            }
        }
        .frame(maxWidth: .infinity)
        .loginRise(Cue.anchor, enabled: introReady)
    }
}
