import UIKit

/// Centralized haptic feedback — call sites read as intent ("the user
/// approved something") instead of re-deriving which generator/style to
/// reach for each time.
///
/// The generators are RETAINED, not built per call. They used to be local
/// lets: created, `prepare()`d, fired and deallocated on three consecutive
/// lines. That works right up until the Taptic Engine is cold, because
/// `prepare()` is asynchronous — it asks the engine to spin up and returns
/// immediately — so firing in the same breath only lands if the engine
/// happens to already be running from a recent tap, and the generator is
/// gone before it could have warmed up anyway.
///
/// Which is exactly why Sign In had no haptic after Password AutoFill:
/// the Face ID / AutoFill flow tears the app's haptic connection down
/// (`com.apple.audio.hapticd ... was invalidated from this process`,
/// straight out of a device log from this app), so the very next tap met a
/// cold engine and a generator that deallocated before it could warm one.
/// Every other button in the app worked because the user had just been
/// tapping, so the engine was already spinning.
///
/// Retained generators keep the engine warm between calls and survive long
/// enough to actually play. `warmUp()` is for the case where a haptic is
/// coming but nothing has been tapped recently — see its own comment.
@MainActor
enum Haptic {
    private static let impactLight = UIImpactFeedbackGenerator(style: .light)
    private static let impactMedium = UIImpactFeedbackGenerator(style: .medium)
    private static let impactHeavy = UIImpactFeedbackGenerator(style: .heavy)
    private static let notification = UINotificationFeedbackGenerator()
    private static let selectionGenerator = UISelectionFeedbackGenerator()

    /// Spin the engine up ahead of a haptic we know is coming, at a moment
    /// where it may be cold — after a system flow that tears the
    /// connection down (Password AutoFill, Face ID), or on a screen the
    /// user has just landed on without tapping anything yet. Cheap, and
    /// the engine idles back down on its own.
    static func warmUp() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        impactLight.prepare()
        impactMedium.prepare()
        notification.prepare()
        selectionGenerator.prepare()
    }

    /// Quick, small tap feedback — buttons, tab switches, picker changes.
    static func light() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        impactLight.impactOccurred()
        // Re-prepare AFTER firing, so the engine is already warm for the
        // next one rather than being asked to warm up at the moment it's
        // needed. This is the ordering Apple's own guidance describes.
        impactLight.prepare()
    }

    /// A slightly heavier tap — reserved for a deliberate, weightier action.
    static func medium() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        impactMedium.impactOccurred()
        impactMedium.prepare()
    }

    /// The heaviest tap — a single deliberate thud for a big, physical
    /// gesture completing (pull-to-refresh engaging). Deliberately not used
    /// for anything smaller than that, or it stops reading as significant.
    static func heavy() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        impactHeavy.prepare()
        impactHeavy.impactOccurred()
    }

    /// A completed action landed successfully (review posted, schedule
    /// generated, campaign sent, Face ID unlocked).
    static func success() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        notification.notificationOccurred(.success)
        notification.prepare()
    }

    /// Something needs the user's attention but isn't a hard failure.
    static func warning() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        notification.notificationOccurred(.warning)
        notification.prepare()
    }

    /// An action failed outright (failed login, failed API call, Face ID
    /// failure) — pairs with the app's existing red error banners.
    static func error() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        notification.notificationOccurred(.error)
        notification.prepare()
    }

    /// A subtle tick for discrete state changes — toggles, swipe actions.
    static func selection() {
        guard AppPreferences.shared.hapticsEnabled else { return }
        selectionGenerator.selectionChanged()
        selectionGenerator.prepare()
    }
}
