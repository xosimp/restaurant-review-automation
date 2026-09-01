import SwiftUI

/// House convention for every text-input modal — an ember icon, a plain
/// TextField/SecureField, and a hairline underline that lights up ember on
/// focus. No boxed/gray backgrounds, matching the free-floating treatment
/// already used for Reviews Analytics' insight lines, instead of the
/// default gray Form-row look every input modal in the app used to have.
struct CavnarFloatingField<Field: Hashable>: View {
    let icon: String
    let placeholder: String
    @Binding var text: String
    var isSecure: Bool = false
    var keyboardType: UIKeyboardType = .default
    var textContentType: UITextContentType?
    var autocapitalization: TextInputAutocapitalization = .sentences
    // Externalized (was a private @FocusState) so a parent screen with
    // several of these fields can drive one shared enum-based FocusState —
    // required for keyboardNavToolbar's up/down chevrons to know which
    // field is active and where "next"/"previous" go. The underline's own
    // lit-on-focus behavior below just reads whether THIS field is the one
    // the shared state currently points at.
    var focus: FocusState<Field?>.Binding
    var field: Field

    private var isFocused: Bool { focus.wrappedValue == field }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(width: 18)
                Group {
                    if isSecure {
                        SecureField(placeholder, text: $text)
                    } else {
                        TextField(placeholder, text: $text)
                    }
                }
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk)
                .keyboardType(keyboardType)
                .textContentType(textContentType)
                .textInputAutocapitalization(autocapitalization)
                .focused(focus, equals: field)
            }
            .padding(.bottom, 12)
            Rectangle()
                .fill(isFocused ? Color.cavnarEmber : Color.cavnarPaper3)
                .frame(height: isFocused ? 1.5 : 1)
        }
        .animation(.easeOut(duration: 0.15), value: isFocused)
    }
}

/// Multi-line counterpart for an optional note/message field — an uppercase
/// caption instead of a leading icon (a note isn't identity-like the way
/// name/email/phone are), a borderless TextEditor with a hand-rolled
/// placeholder (TextEditor has no native one), and the same focus-lit
/// underline as CavnarFloatingField.
struct CavnarFloatingTextArea<Field: Hashable>: View {
    let caption: String
    let placeholder: String
    @Binding var text: String
    var minHeight: CGFloat = 60
    // See CavnarFloatingField's matching comment — externalized for the
    // same shared-FocusState/keyboardNavToolbar reason.
    var focus: FocusState<Field?>.Binding
    var field: Field

    private var isFocused: Bool { focus.wrappedValue == field }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(caption)
                .font(.cavnarBody(12, weight: 700))
                .tracking(0.9)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(alignment: .leading, spacing: 0) {
                ZStack(alignment: .topLeading) {
                    if text.isEmpty {
                        Text(placeholder)
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk3)
                            .padding(.top, 8)
                            .padding(.leading, 5)
                            .allowsHitTesting(false)
                    }
                    TextEditor(text: $text)
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk)
                        .scrollContentBackground(.hidden)
                        .frame(minHeight: minHeight)
                        .focused(focus, equals: field)
                }
                Rectangle()
                    .fill(isFocused ? Color.cavnarEmber : Color.cavnarPaper3)
                    .frame(height: isFocused ? 1.5 : 1)
            }
        }
        .animation(.easeOut(duration: 0.15), value: isFocused)
    }
}
