import Foundation

/// The two headers every request to Cavnar AI carries (release-safety audit
/// #25, A06-02): which build is asking, so the server can tell which builds
/// are installed, keep each login's build, and turn away one too old to read
/// its answers (426, `AppUpdateGate`).
///
///     X-Cavnar-App: 1.0 (4210)      CFBundleShortVersionString (CFBundleVersion)
///     X-Cavnar-Platform: ios
///
/// The build number is stamped by ios/CavnarAI/generate.sh from
/// `git rev-list --count HEAD`, so it only rises. Compiled into the app and
/// the share extension (project.yml); every request builder stamps it —
/// APIClient.buildRequest, StaffTasksAPI, the task-sheet proof photo, the
/// share upload. The server's half is client_compat.py.
enum ClientIdentity {
    static let appHeader = "X-Cavnar-App"
    static let platformHeader = "X-Cavnar-Platform"
    static let platform = "ios"

    /// "<version> (<build>)" from an Info.plist dictionary. A missing or
    /// non-numeric build is 0 — an unknown build reads as the oldest, never
    /// as a new one.
    static func value(info: [String: Any]?) -> String {
        let rawVersion = (info?["CFBundleShortVersionString"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let version = rawVersion.isEmpty || rawVersion.contains(" ") ? "0" : String(rawVersion.prefix(24))
        return "\(version) (\(build(info: info)))"
    }

    static func build(info: [String: Any]?) -> Int {
        let raw = (info?["CFBundleVersion"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return Int(raw) ?? 0
    }

    /// This binary's value, read once.
    static let appValue: String = value(info: Bundle.main.infoDictionary)
    static let buildNumber: Int = build(info: Bundle.main.infoDictionary)

    /// Sets both headers on `request`.
    static func stamp(_ request: inout URLRequest) {
        request.setValue(appValue, forHTTPHeaderField: appHeader)
        request.setValue(platform, forHTTPHeaderField: platformHeader)
    }
}
