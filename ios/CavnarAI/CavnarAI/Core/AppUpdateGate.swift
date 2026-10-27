import Foundation
import Observation

/// The server's "this build is too old to keep working" — a 426 with
/// `{update_required: true, message, min_build, app_store_url}`
/// (client_compat.update_body, release-safety audit #25 A06-02). Builds from
/// before this existed show the same answer's `error` sentence like any
/// refusal; this one shows the blocking sheet (`AppUpdateRequiredView`).
struct AppUpdateRequirement: Equatable, Sendable {
    static let fallbackMessage = "This version of Cavnar AI is too old to keep working. "
        + "Update it in the App Store to carry on."

    let message: String
    let minBuild: Int?
    /// Where the update is, when the server named a place this app may open:
    /// the App Store or TestFlight, nothing else (`trustedStoreURL`).
    let storeURL: URL?

    private struct Body: Decodable {
        let updateRequired: Bool?
        let message: String?
        let error: String?
        let minBuild: Int?
        let appStoreUrl: String?

        enum CodingKeys: String, CodingKey {
            case message, error
            case updateRequired = "update_required"
            case minBuild = "min_build"
            case appStoreUrl = "app_store_url"
        }
    }

    /// The requirement a response states, or nil. Any 426 is one (the HTTP
    /// status means exactly this); a body that says `update_required` with
    /// another status is one too.
    static func from(status: Int, body: Data) -> AppUpdateRequirement? {
        let decoded = try? JSONDecoder.cavnar.decode(Body.self, from: body)
        guard status == 426 || decoded?.updateRequired == true else { return nil }
        let words = [decoded?.message, decoded?.error]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { !$0.isEmpty }
        return AppUpdateRequirement(message: words ?? fallbackMessage, minBuild: decoded?.minBuild,
                                    storeURL: trustedStoreURL(decoded?.appStoreUrl))
    }

    /// A store link from the server, only when it is the App Store or
    /// TestFlight — a server field is never opened as an arbitrary URL
    /// (security report 1.4: a swapped field must not open a phishing page).
    static func trustedStoreURL(_ raw: String?) -> URL? {
        guard let raw, let url = URL(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = url.scheme?.lowercased() else { return nil }
        if scheme == "itms-apps" || scheme == "itms-beta" { return url }
        guard scheme == "https", let host = url.host?.lowercased() else { return nil }
        return ["apps.apple.com", "testflight.apple.com", "itunes.apple.com"].contains(host) ? url : nil
    }

    /// What the sheet's button opens: the server's link, else the App Store.
    var openURL: URL {
        storeURL ?? URL(string: "itms-apps://apps.apple.com/")!
    }
}

/// Holds the requirement once any response stated one, for RootView's
/// blocking sheet — until the server stops saying it. The build does not
/// get newer while the process runs, but the server's minimum can come DOWN:
/// a mistyped MOBILE_MIN_BUILD, fixed and redeployed, used to leave every
/// phone on the sheet until the person force-quit the app (blind re-audit
/// 10/27/26). So while the sheet is up it asks again — when the app comes
/// back to the foreground, every `recheckInterval`, and on "Check again" —
/// and opens the moment the answer is anything but 426.
@Observable
@MainActor
final class AppUpdateGate {
    static let shared = AppUpdateGate()
    /// How often the sheet asks again while it is on screen.
    static let recheckInterval: TimeInterval = 300

    private(set) var requirement: AppUpdateRequirement?
    /// A re-check is on its way (the sheet shows the pulse).
    private(set) var isChecking = false

    func require(_ requirement: AppUpdateRequirement) {
        if self.requirement == nil { self.requirement = requirement }
    }

    /// The server answered something other than 426: the build is served
    /// again, and the sheet goes.
    func clear() {
        requirement = nil
    }

    /// Asks `probe` whether the server still turns this build away (true),
    /// serves it (false — the gate clears) or could not be asked (nil — it
    /// stays). One at a time; nothing to do while no requirement is held.
    /// The production probe is APIClient.stillRequiresUpdate.
    func recheck(_ probe: @Sendable () async -> Bool?) async {
        guard requirement != nil, !isChecking else { return }
        isChecking = true
        defer { isChecking = false }
        if await probe() == false {
            clear()
        }
    }

    /// The real re-check, against this app's server.
    func recheck() async {
        await recheck { await APIClient.shared.stillRequiresUpdate() }
    }

    /// Tests only: a fresh gate between cases.
    func resetForTesting() {
        requirement = nil
        isChecking = false
    }
}
