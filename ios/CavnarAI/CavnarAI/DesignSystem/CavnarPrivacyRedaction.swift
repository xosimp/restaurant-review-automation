import SwiftUI

/// Privacy mode (Security -> This device): the big dollar figures and KPI
/// numbers stay blurred for as long as the setting is on. No tap-to-reveal
/// — for the app sitting open on the pass, or handed to a server, "hidden
/// unless someone taps it" isn't hidden. Turn the setting off to see them.
private struct CavnarSensitive: ViewModifier {
    @State private var prefs = AppPreferences.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .blur(radius: prefs.privacyMode ? 9 : 0)
            .opacity(prefs.privacyMode ? 0.55 : 1)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.25), value: prefs.privacyMode)
            .accessibilityHidden(prefs.privacyMode)
    }
}

extension View {
    /// Mark a figure as sensitive — blurred whenever privacy mode is on.
    func cavnarSensitive() -> some View { modifier(CavnarSensitive()) }
}
