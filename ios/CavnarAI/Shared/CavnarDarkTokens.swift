import SwiftUI
import UIKit

/// Cavnar AI is dark only (parity audit #18). A view can be held dark with
/// `.environment(\.colorScheme, .dark)` — every widget entry view and Live
/// Activity view is (`cavnarForcedDark()`). A few system surfaces take a
/// Color as a VALUE instead of drawing a view — a Live Activity's background
/// tint, its system action colour — and an environment override never
/// reaches those, so on a phone set to light mode the asset catalog's light
/// appearance (Paper #F7F4EF) was what they drew. These resolve the same
/// token in its dark appearance, whatever the phone is set to.
extension Color {
    static func cavnarDarkToken(_ name: String) -> Color {
        guard let ui = UIColor(named: name) else { return Color(name) }
        return Color(uiColor: ui.resolvedColor(with: UITraitCollection(userInterfaceStyle: .dark)))
    }

    static var cavnarPaperDark: Color { cavnarDarkToken("Paper") }
    static var cavnarInkDark: Color { cavnarDarkToken("Ink") }
}

extension View {
    /// Draws this widget or Live Activity view in the dark palette on any
    /// phone (#18).
    func cavnarForcedDark() -> some View {
        environment(\.colorScheme, .dark)
    }
}
