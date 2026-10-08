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
            if #available(iOS 26.0, *) {
                // iOS 26 sizes every keyboard item to its content and centres
                // the lot, so a Spacer inside one HStack had no width to take
                // and the chevrons and checkmark sat together mid-screen.
                // Separate items with Apple's ToolbarSpacer between them put
                // the chevrons at the leading edge and the checkmark at the
                // trailing one, as every system app does.
                ToolbarItem(placement: .keyboard) { chevrons }
                    .sharedBackgroundVisibility(.hidden)
                ToolbarSpacer(.flexible, placement: .keyboard)
                ToolbarItem(placement: .keyboard) { done }
                    .sharedBackgroundVisibility(.hidden)
            } else {
                // One HStack, not separate top-level items — a bare
                // Spacer() as its own sibling item inside a .keyboard
                // ToolbarItemGroup misbehaves below iOS 26.
                ToolbarItemGroup(placement: .keyboard) {
                    HStack(spacing: 8) {
                        chevrons
                        Spacer()
                        done
                    }
                }
            }
        }
    }

    private var chevrons: some View {
        HStack(spacing: 8) {
            keyboardIconButton(systemName: "chevron.up", enabled: canGoPrevious) {
                if let i = currentIndex, i > 0 { focus.wrappedValue = all[i - 1] }
            }
            keyboardIconButton(systemName: "chevron.down", enabled: canGoNext) {
                if let i = currentIndex, i < all.count - 1 { focus.wrappedValue = all[i + 1] }
            }
        }
    }

    private var done: some View {
        keyboardIconButton(systemName: "checkmark", enabled: true) {
            focus.wrappedValue = nil
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
            cavnarKeyboardTrailing {
                keyboardIconButton(systemName: "checkmark", enabled: true, action: onDone)
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
            .font(.system(size: 14, weight: .bold))
            .foregroundStyle(enabled ? Color.white : Color.cavnarInk3)
            .frame(width: 34, height: 34)
            // Opaque, not the 14% ember wash the other toolbar icons use.
            // On iOS 26 the keyboard toolbar has no bar of its own (see
            // cavnarToolbarItemGroup — the shared glass is hidden so it
            // stops double-wrapping) — these buttons float straight over
            // whatever the page has scrolled under the keyboard, and a
            // translucent circle over a header row or a share icon was
            // unreadable and looked like a stray overlay. A solid fill with
            // a hairline and a drop shadow reads as a button on top of
            // anything.
            .background(enabled ? Color.cavnarEmber : Color.cavnarPaper3, in: Circle())
            .overlay(Circle().strokeBorder(Color.white.opacity(enabled ? 0.22 : 0.08), lineWidth: 1))
            .shadow(color: .black.opacity(0.45), radius: 6, y: 3)
            .contentShape(Circle())
    }
    .disabled(!enabled)
    .fixedSize()
    .buttonStyle(.plain)
    .tint(nil)
}

/// A keyboard bar whose one control sits at the trailing edge, as in every
/// system app: on iOS 26+ a ToolbarSpacer and its own item (a Spacer inside
/// one item has no width to take there, so the control landed mid-screen);
/// below, the classic Spacer in the group.
@ToolbarContentBuilder
func cavnarKeyboardTrailing<Content: View>(@ViewBuilder _ content: () -> Content) -> some ToolbarContent {
    if #available(iOS 26.0, *) {
        ToolbarSpacer(.flexible, placement: .keyboard)
        ToolbarItem(placement: .keyboard, content: content)
            .sharedBackgroundVisibility(.hidden)
    } else {
        ToolbarItemGroup(placement: .keyboard) {
            HStack {
                Spacer()
                content()
            }
        }
    }
}

extension View {
    func keyboardNavToolbar<Field: Hashable & CaseIterable>(_ focus: FocusState<Field?>.Binding) -> some View {
        modifier(KeyboardNavToolbarModifier(focus: focus))
    }

    func keyboardDoneToolbar(onDone: @escaping () -> Void) -> some View {
        modifier(KeyboardDoneToolbarModifier(onDone: onDone))
    }
}
