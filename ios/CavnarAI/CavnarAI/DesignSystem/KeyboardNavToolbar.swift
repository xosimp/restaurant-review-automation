import SwiftUI

/// Apple's own password-AutoFill keyboard bar (up/down chevrons to move
/// between fields, a checkmark in place of "Done") applied everywhere a
/// keyboard appears in this app, instead of the system's plain default —
/// or, on screens that build their own custom bar already (Food Cost's
/// carousel), the same checkmark glyph swapped in for whatever "Done" used
/// to say there.
///
/// Both variants build on cavnarToolbarItemGroup/cavnarToolbarIconGlass —
/// the fix already in place for iOS 26's shared-glass-wrapper toolbar bug
/// (see their doc comments in this file). Re-deriving the toolbar
/// mechanics per screen would silently reintroduce that same bug; every
/// call site below routes through these two.

/// Multi-field variant — Field.allCases order must match on-screen order
/// (top to bottom) since that's what the chevrons step through.
private struct KeyboardNavToolbarModifier<Field: Hashable & CaseIterable>: ViewModifier {
    var focus: FocusState<Field?>.Binding

    private var all: [Field] { Array(Field.allCases) }
    private var currentIndex: Int? {
        focus.wrappedValue.flatMap { all.firstIndex(of: $0) }
    }
    private var canGoPrevious: Bool { (currentIndex ?? 0) > 0 }
    private var canGoNext: Bool {
        guard let i = currentIndex else { return false }
        return i < all.count - 1
    }

    func body(content: Content) -> some View {
        content.toolbar {
            cavnarToolbarItemGroup(placement: .keyboard) {
                // One HStack, not separate top-level items — a bare
                // Spacer() as its own sibling item inside a .keyboard
                // ToolbarItemGroup independently triggers the same iOS 26
                // layout bug cavnarToolbarItemGroup works around at the
                // group level (see FoodCostQuickEntryView's identical fix).
                HStack(spacing: 8) {
                    keyboardIconButton(systemName: "chevron.up", enabled: canGoPrevious) {
                        if let i = currentIndex, i > 0 { focus.wrappedValue = all[i - 1] }
                    }
                    keyboardIconButton(systemName: "chevron.down", enabled: canGoNext) {
                        if let i = currentIndex, i < all.count - 1 { focus.wrappedValue = all[i + 1] }
                    }
                    Spacer()
                    keyboardIconButton(systemName: "checkmark", enabled: true) {
                        focus.wrappedValue = nil
                    }
                }
            }
        }
    }
}

/// Single-field (or dynamic-field-count) variant — just the checkmark,
/// chevrons wouldn't have anywhere to go. Used for one-field screens
/// (2FA code, Ask Cavnar's compose box) and screens whose field set is
/// built per-row at runtime rather than a fixed CaseIterable enum (Food
/// Cost's ingredient carousel).
private struct KeyboardDoneToolbarModifier: ViewModifier {
    var onDone: () -> Void

    func body(content: Content) -> some View {
        content.toolbar {
            cavnarToolbarItemGroup(placement: .keyboard) {
                HStack {
                    Spacer()
                    keyboardIconButton(systemName: "checkmark", enabled: true, action: onDone)
                }
            }
        }
    }
}

/// Not private — FoodCostQuickEntryView's own hand-rolled keyboard toolbar
/// (a dynamic per-ingredient-card field set that doesn't fit the
/// CaseIterable-enum shape the two modifiers above need) reuses this
/// directly for its own checkmark button, so that one dismiss glyph stays
/// pixel-identical to every other keyboard toolbar in the app.
/// @MainActor throughout: this is a SwiftUI view builder, so it is main-actor
/// work by definition, and its action closure legitimately touches main-actor
/// state (focus bindings). Annotating the closure @MainActor rather than
/// @Sendable is what actually resolves the strict-concurrency complaint —
/// @Sendable just moves it onto every caller.
@MainActor
@ViewBuilder
func keyboardIconButton(systemName: String, enabled: Bool, action: @escaping @MainActor () -> Void) -> some View {
    Button {
        Haptic.light()
        action()
    } label: {
        Image(systemName: systemName)
            .font(.system(size: 13, weight: .semibold))
    }
    .disabled(!enabled)
    .foregroundStyle(enabled ? Color.cavnarEmber : Color.cavnarInk3)
    .fixedSize()
    .cavnarToolbarIconGlass(size: 30)
    .buttonStyle(.plain)
    .tint(nil)
}

extension View {
    func keyboardNavToolbar<Field: Hashable & CaseIterable>(_ focus: FocusState<Field?>.Binding) -> some View {
        modifier(KeyboardNavToolbarModifier(focus: focus))
    }

    func keyboardDoneToolbar(onDone: @escaping () -> Void) -> some View {
        modifier(KeyboardDoneToolbarModifier(onDone: onDone))
    }
}
