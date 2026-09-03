import SwiftUI

/// The auth screens' text field: a glass card with a leading SF Symbol,
/// an ember underline that sweeps in on focus, an optional show/hide eye
/// (a full 44pt target), and a red border + shake when the form errors.
/// Built on the same focus-lit underline idea as CavnarFloatingField, in
/// the taller card form the sign-in design uses.
struct LoginField<Field: Hashable>: View {
    let systemImage: String
    let placeholder: String
    @Binding var text: String
    var isSecure: Bool = false
    var keyboardType: UIKeyboardType = .default
    var textContentType: UITextContentType? = nil
    var autocapitalization: TextInputAutocapitalization = .never
    var submitLabel: SubmitLabel = .next
    var onSubmit: (() -> Void)? = nil
    var focus: FocusState<Field?>.Binding
    let field: Field
    // Bumps to trigger the shake; the red border shows while `isError`.
    var isError: Bool = false
    var shakeTrigger: Int = 0

    @State private var revealed = false
    @State private var shake: CGFloat = 0

    private var isFocused: Bool { focus.wrappedValue == field }

    var body: some View {
        HStack(spacing: LoginMetrics.spaceM) {
            Image(systemName: systemImage)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(isFocused ? Color.cavnarEmber2 : Color.cavnarInk3)
                .frame(width: 20)
                .animation(.easeOut(duration: 0.2), value: isFocused)

            Group {
                if isSecure && !revealed {
                    SecureField(placeholder, text: $text)
                } else {
                    TextField(placeholder, text: $text)
                }
            }
            .font(.cavnarBody(16, weight: 600))
            .foregroundStyle(Color.cavnarInk)
            .keyboardType(keyboardType)
            .textContentType(textContentType)
            .textInputAutocapitalization(autocapitalization)
            .autocorrectionDisabled()
            .submitLabel(submitLabel)
            .onSubmit { onSubmit?() }
            .focused(focus, equals: field)

            if isSecure {
                Button {
                    Haptic.selection()
                    revealed.toggle()
                } label: {
                    Image(systemName: revealed ? "eye.slash" : "eye")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(width: LoginMetrics.touch, height: LoginMetrics.touch)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .padding(.trailing, -LoginMetrics.spaceM)
            }
        }
        .padding(.horizontal, LoginMetrics.spaceL)
        .frame(height: LoginMetrics.fieldHeight)
        .background(Color.cavnarPaper2.opacity(0.62), in: RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .strokeBorder(borderColor, lineWidth: 1)
        )
        .overlay(alignment: .bottom) {
            // The ember underline — inset to the text's own left edge, and
            // only ever lit while this field has focus.
            Rectangle()
                .fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber.opacity(0)], startPoint: .leading, endPoint: .trailing))
                .frame(height: 1.5)
                .padding(.horizontal, LoginMetrics.spaceL)
                .scaleEffect(x: isFocused ? 1 : 0, anchor: .leading)
                .animation(.easeOut(duration: 0.25), value: isFocused)
        }
        .shadow(color: Color.cavnarEmber.opacity(isFocused ? 0.10 : 0), radius: 6)
        .contentShape(Rectangle())
        .onTapGesture { focus.wrappedValue = field }
        .offset(x: shake)
        .onChange(of: shakeTrigger) { _, _ in
            guard isError else { return }
            // Same decaying side-to-side shake iOS uses for a wrong passcode.
            let steps: [CGFloat] = [-4, 4, -4, 4, -2, 2, 0]
            Task {
                for s in steps {
                    withAnimation(.linear(duration: 0.05)) { shake = s }
                    try? await Task.sleep(for: .milliseconds(50))
                }
            }
        }
    }

    private var borderColor: Color {
        if isError { return Color.cavnarRed.opacity(0.6) }
        if isFocused { return Color.cavnarEmber.opacity(0.55) }
        return Color.cavnarPaper3.opacity(0.7)
    }
}

/// The inline error strip under a form — red tint, exclamation, the
/// message. Paired with Haptic.error() at the call site that sets it.
struct LoginErrorBar: View {
    let message: String

    var body: some View {
        HStack(spacing: LoginMetrics.spaceS) {
            Image(systemName: "exclamationmark.circle.fill")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Color.cavnarRed)
            Text(message)
                .font(.cavnarBody(13.5, weight: 600))
                .foregroundStyle(Color.cavnarRed.opacity(0.92))
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, LoginMetrics.spaceM)
        .padding(.vertical, 10)
        .background(Color.cavnarRed.opacity(0.10), in: RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                .strokeBorder(Color.cavnarRed.opacity(0.35), lineWidth: 1)
        )
        .transition(.opacity.combined(with: .move(edge: .top)))
    }
}

/// The two social buttons — white pills per Apple's dark-background
/// guideline (and Google's own, for their mark), 52pt like every other
/// control on the form.
struct LoginSocialButton<Icon: View>: View {
    let title: String
    var isLoading: Bool
    @ViewBuilder var icon: () -> Icon
    var action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: 10) {
                icon().frame(width: 18, height: 18)
                Text(title)
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(.black)
            }
            .frame(maxWidth: .infinity)
            .frame(height: LoginMetrics.buttonHeight)
            .background(.white, in: RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
            .contentShape(Rectangle())
        }
        .buttonStyle(LoginPressStyle())
        .disabled(isLoading)
        .opacity(isLoading ? 0.55 : 1)
    }
}

/// Press feedback for the social pills — a settle, no haptic of its own
/// (the action closure fires Haptic.light() so there's exactly one).
struct LoginPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .opacity(configuration.isPressed ? 0.9 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// The sign-in design's scale — every number on these screens comes from
/// here, not from a literal at the call site.
enum LoginMetrics {
    static let spaceXS: CGFloat = 4
    static let spaceS: CGFloat = 8
    static let spaceM: CGFloat = 12
    static let spaceL: CGFloat = 16
    static let spaceXL: CGFloat = 24
    static let spaceXXL: CGFloat = 32
    static let spaceHero: CGFloat = 48
    static let touch: CGFloat = 44
    static let fieldHeight: CGFloat = 52
    static let buttonHeight: CGFloat = 52
    static let wordmarkWidth: CGFloat = 236
    static let pageInset: CGFloat = 24
}
