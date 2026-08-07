import UIKit

/// Centralized haptic feedback — call sites read as intent ("the user
/// approved something") instead of re-deriving which generator/style to
/// reach for each time. A fresh generator per call, prepare()'d
/// immediately before triggering, is Apple's own recommended pattern for
/// minimizing the latency between the triggering action and the buzz.
@MainActor
enum Haptic {
    /// Quick, small tap feedback — buttons, tab switches, picker changes.
    static func light() {
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.prepare()
        generator.impactOccurred()
    }

    /// A slightly heavier tap — reserved for a deliberate, weightier action.
    static func medium() {
        let generator = UIImpactFeedbackGenerator(style: .medium)
        generator.prepare()
        generator.impactOccurred()
    }

    /// A completed action landed successfully (review posted, schedule
    /// generated, campaign sent, Face ID unlocked).
    static func success() {
        let generator = UINotificationFeedbackGenerator()
        generator.prepare()
        generator.notificationOccurred(.success)
    }

    /// Something needs the user's attention but isn't a hard failure.
    static func warning() {
        let generator = UINotificationFeedbackGenerator()
        generator.prepare()
        generator.notificationOccurred(.warning)
    }

    /// An action failed outright (failed login, failed API call, Face ID
    /// failure) — pairs with the app's existing red error banners.
    static func error() {
        let generator = UINotificationFeedbackGenerator()
        generator.prepare()
        generator.notificationOccurred(.error)
    }

    /// A subtle tick for discrete state changes — toggles, swipe actions.
    static func selection() {
        let generator = UISelectionFeedbackGenerator()
        generator.prepare()
        generator.selectionChanged()
    }
}
