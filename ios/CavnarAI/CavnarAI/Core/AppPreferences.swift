import Foundation
import Observation

/// Device-local app preferences (Account -> App / Security -> This device).
/// UserDefaults-backed — none of these are secrets and none sync to the
/// backend; they describe how THIS phone behaves. Observable so the rows
/// that edit them and the views that honour them re-render together.
@Observable
@MainActor
final class AppPreferences {
    static let shared = AppPreferences()

    private enum Key {
        static let haptics = "cavnar.pref.haptics"
        static let defaultTab = "cavnar.pref.default_tab"
        static let lockDelay = "cavnar.pref.lock_delay"
        static let privacyMode = "cavnar.pref.privacy_mode"
    }

    /// Haptic.* and every .sensoryFeedback site check this.
    var hapticsEnabled: Bool {
        didSet { UserDefaults.standard.set(hapticsEnabled, forKey: Key.haptics) }
    }

    /// Where the app lands after sign-in / unlock.
    var defaultTab: AppTab {
        didSet { UserDefaults.standard.set(defaultTab.rawValue, forKey: Key.defaultTab) }
    }

    /// Seconds in the background before the re-entry lock engages. 0 =
    /// immediately (the original behaviour). See SessionStore.noteBackgrounded.
    var lockDelaySeconds: Int {
        didSet { UserDefaults.standard.set(lockDelaySeconds, forKey: Key.lockDelay) }
    }

    /// Masks dollar figures and KPI numbers until tapped — for an app
    /// that's open on the pass or handed to a server. See
    /// View.cavnarSensitive().
    var privacyMode: Bool {
        didSet { UserDefaults.standard.set(privacyMode, forKey: Key.privacyMode) }
    }

    static let lockDelayOptions: [(seconds: Int, label: String)] = [
        (0, "Immediately"), (60, "After 1 minute"), (300, "After 5 minutes"), (900, "After 15 minutes"),
    ]

    private init() {
        let d = UserDefaults.standard
        hapticsEnabled = d.object(forKey: Key.haptics) == nil ? true : d.bool(forKey: Key.haptics)
        defaultTab = AppTab(rawValue: d.string(forKey: Key.defaultTab) ?? "") ?? .home
        lockDelaySeconds = d.integer(forKey: Key.lockDelay)
        privacyMode = d.bool(forKey: Key.privacyMode)
    }

    /// Non-isolated read for the few call sites that can't hop to the main
    /// actor (e.g. a ButtonStyle's sensoryFeedback condition).
    nonisolated static var hapticsEnabledSnapshot: Bool {
        UserDefaults.standard.object(forKey: Key.haptics) == nil ? true : UserDefaults.standard.bool(forKey: Key.haptics)
    }
}
