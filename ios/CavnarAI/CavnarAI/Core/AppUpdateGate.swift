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
/// blocking sheet. It is never cleared while the process runs: the build
/// does not get newer until the person updates it.
@Observable
@MainActor
final class AppUpdateGate {
    static let shared = AppUpdateGate()

    private(set) var requirement: AppUpdateRequirement?

    func require(_ requirement: AppUpdateRequirement) {
        if self.requirement == nil { self.requirement = requirement }
    }

    /// Tests only: a fresh gate between cases.
    func resetForTesting() {
        requirement = nil
    }
}
