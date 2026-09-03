import Foundation

/// Disk cache for anything containing staff PII or restaurant financials.
///
/// Replaces the UserDefaults caching the Labor/Food Cost view models used to
/// do. UserDefaults is an unencrypted plist in the app container with no
/// data-protection class: readable from an unencrypted device backup, and
/// readable on a jailbroken device even while it's locked. Generated
/// schedules contain employee names, shift assignments and wage figures, so
/// they do not belong there (audit 1.2).
///
/// Files written here use `.completeFileProtection` — encrypted at rest and
/// inaccessible while the device is locked — and `purgeAll()` is called from
/// SessionStore.clearLocalSession() so a signed-out device does not keep the
/// previous account's data on disk.
enum SecureCache {
    private static let directoryName = "SecureCache"

    private static var directory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent(directoryName, isDirectory: true)
        if !FileManager.default.fileExists(atPath: dir.path) {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir
    }

    /// Keys arrive from callers as dotted identifiers ("labor.cachedStats.4"),
    /// which are already filename-safe, but sanitize anyway so a future key
    /// containing a path separator can't escape the directory.
    private static func fileURL(for key: String) -> URL {
        let safe = key.replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "..", with: "_")
        return directory.appendingPathComponent(safe)
    }

    static func write(_ data: Data, key: String) {
        try? data.write(to: fileURL(for: key), options: .completeFileProtection)
    }

    static func read(key: String) -> Data? {
        try? Data(contentsOf: fileURL(for: key))
    }

    static func delete(key: String) {
        try? FileManager.default.removeItem(at: fileURL(for: key))
    }

    /// Every cached blob, dropped. Called on sign-out: the next person to use
    /// this device must not inherit the previous restaurant's schedules,
    /// labor costs or AI insights.
    static func purgeAll() {
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil
        ) else { return }
        for file in files { try? FileManager.default.removeItem(at: file) }
    }

    /// When `key` was last written, used to tell the user how old cached
    /// numbers are rather than presenting them as live (audit 6.5).
    static func modifiedAt(key: String) -> Date? {
        try? FileManager.default.attributesOfItem(atPath: fileURL(for: key).path)[.modificationDate] as? Date
    }
}
