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
        guard AppPreferences.shared.hapticsEnabled else { return }
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.prepare()
        generator.impactOccurred()
    }

    /// A slightly heavier tap — reserved for a deliberate, weightier action.
    static func medium() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        let generator = UIImpactFeedbackGenerator(style: .medium)
        generator.prepare()
        generator.impactOccurred()
    }

    /// The heaviest tap — a single deliberate thud for a big, physical
    /// gesture completing (pull-to-refresh engaging). Deliberately not used
    /// for anything smaller than that, or it stops reading as significant.
    static func heavy() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        let generator = UIImpactFeedbackGenerator(style: .heavy)
        generator.prepare()
        generator.impactOccurred()
    }

    /// A completed action landed successfully (review posted, schedule
    /// generated, campaign sent, Face ID unlocked).
    static func success() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        let generator = UINotificationFeedbackGenerator()
        generator.prepare()
        generator.notificationOccurred(.success)
    }

    /// Something needs the user's attention but isn't a hard failure.
    static func warning() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        let generator = UINotificationFeedbackGenerator()
        generator.prepare()
        generator.notificationOccurred(.warning)
    }

    /// An action failed outright (failed login, failed API call, Face ID
    /// failure) — pairs with the app's existing red error banners.
    static func error() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        let generator = UINotificationFeedbackGenerator()
        generator.prepare()
        generator.notificationOccurred(.error)
    }

    /// A subtle tick for discrete state changes — toggles, swipe actions.
    static func selection() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        let generator = UISelectionFeedbackGenerator()
        generator.prepare()
        generator.selectionChanged()
    }
}
