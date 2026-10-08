import SwiftUI
import UIKit

// The iPad rules (parity audit #99, 10/7/26). One place decides when the
// app is wide: the horizontal size class. Regular (an iPad full screen, or
// the wide side of Split View) gets the sidebar, readable columns and the
// two-up layouts; compact (every iPhone, and an iPad narrowed in Split View
// or Slide Over) keeps the phone exactly as it is. DESIGN_SYSTEM.md → iPad.

enum CavnarLayout {
    /// The widest a single column of reading runs on a wide screen —
    /// Home, a review, the nightly report, Account's sheets, Ask.
    static let readableWidth: CGFloat = 720

    /// The sidebar (NavigationSplitView) replaces the tab bar only on a
    /// regular width. Nil (no size class yet) is the phone shape.
    static func usesSidebar(_ sizeClass: UserInterfaceSizeClass?) -> Bool {
        sizeClass == .regular
    }

    /// Wide enough for two columns, a readable width, a week grid.
    static func isWide(_ sizeClass: UserInterfaceSizeClass?) -> Bool {
        sizeClass == .regular
    }

    /// Sheets are form-sized on an iPad; an iPhone's sheets are untouched.
    static func usesFormSheet(idiom: UIUserInterfaceIdiom) -> Bool {
        idiom == .pad
    }
}

// MARK: - Readable width

private struct CavnarReadableWidth: ViewModifier {
    @Environment(\.horizontalSizeClass) private var sizeClass
    let maxWidth: CGFloat

    func body(content: Content) -> some View {
        // A flexible frame with no constraint takes its child's size, so the
        // compact branch is layout-neutral and the view keeps one identity
        // when Split View flips the size class.
        let wide = CavnarLayout.isWide(sizeClass)
        content
            .frame(maxWidth: wide ? maxWidth : nil)
            .frame(maxWidth: wide ? .infinity : nil)
    }
}

extension View {
    /// Centres a long single column at `CavnarLayout.readableWidth` on a
    /// regular width; a no-op on the phone.
    func cavnarReadableWidth(_ maxWidth: CGFloat = CavnarLayout.readableWidth) -> some View {
        modifier(CavnarReadableWidth(maxWidth: maxWidth))
    }
}

// MARK: - Two up

/// Two blocks side by side on a regular width, stacked on the phone —
/// for pairs that read as equals (the waste ledger beside tied-up capital).
struct CavnarTwoUp<Leading: View, Trailing: View>: View {
    var spacing: CGFloat = 34
    @ViewBuilder let leading: () -> Leading
    @ViewBuilder let trailing: () -> Trailing
    @Environment(\.horizontalSizeClass) private var sizeClass

    var body: some View {
        if CavnarLayout.isWide(sizeClass) {
            HStack(alignment: .top, spacing: 24) {
                leading().frame(maxWidth: .infinity, alignment: .topLeading)
                trailing().frame(maxWidth: .infinity, alignment: .topLeading)
            }
        } else {
            VStack(alignment: .leading, spacing: spacing) {
                leading()
                trailing()
            }
        }
    }
}

// MARK: - Sheets

private struct CavnarFormSheet: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 18.0, *), CavnarLayout.usesFormSheet(idiom: UIDevice.current.userInterfaceIdiom) {
            content.presentationSizing(.form)
        } else {
            content
        }
    }
}

extension View {
    /// A sheet that is a centred form on an iPad (iOS 18's
    /// `presentationSizing(.form)`), never a stretched phone sheet. Applied
    /// inside the sheet's content; the phone keeps its detents unchanged.
    func cavnarFormSheet() -> some View {
        modifier(CavnarFormSheet())
    }
}

// MARK: - Pointer

extension View {
    /// The pointer's lift on a primary tappable card (iPad trackpad/mouse).
    /// Nothing on the phone. Only for things that open or act — a card
    /// that is only read gets no hover.
    func cavnarHoverCard(cornerRadius: CGFloat = CavnarRadius.card) -> some View {
        self
            .contentShape(.hoverEffect, RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .hoverEffect(.lift)
    }
}

// MARK: - Keyboard

/// What a hardware keyboard asks of the app. Tab switching (⌘1…) and
/// ⌘K / ⌘N are handled by the shell that owns the tabs; ⌘R refreshes the
/// screen on top through its own pull-to-refresh (CavnarEmberRefreshable).
enum CavnarKeyCommand {
    static let refresh = Notification.Name("cavnarKeyboardRefresh")
}

/// Invisible buttons that carry keyboard shortcuts. A Button with a
/// `.keyboardShortcut` anywhere in the tree registers the key command and
/// lists it in the iPad's ⌘ overlay; these draw nothing and take no taps.
struct CavnarShortcutButton: View {
    let title: String
    let key: KeyEquivalent
    var modifiers: EventModifiers = .command
    let action: () -> Void

    var body: some View {
        Button(title, action: action)
            .keyboardShortcut(key, modifiers: modifiers)
            .frame(width: 0, height: 0)
            .opacity(0)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}
