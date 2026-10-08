import SwiftUI

/// Handoff to the web (parity audit #97): the module on screen is offered to
/// the owner's Mac or iPad as its dashboard.cavnar.ai page — the same
/// `/?nav=<path>` links every email and push already carry (morning_brief,
/// notify, nav.py), so the browser opens exactly this place.
enum CavnarHandoff {
    /// Declared in Info.plist's NSUserActivityTypes (project.yml).
    static let activityType = "ai.cavnar.CavnarAI.browsing"
    static let webBase = "https://dashboard.cavnar.ai/"

    /// https://dashboard.cavnar.ai/?nav=labor/schedule — nil for an empty path.
    static func webpageURL(for navPath: String) -> URL? {
        let path = navPath.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        guard !path.isEmpty else { return nil }
        var c = URLComponents(string: webBase)
        c?.queryItems = [URLQueryItem(name: "nav", value: path)]
        return c?.url
    }
}

extension View {
    /// Advertises this screen for Handoff as its web page.
    func cavnarHandoff(_ navPath: String) -> some View {
        userActivity(CavnarHandoff.activityType) { activity in
            activity.title = "Cavnar AI"
            activity.isEligibleForHandoff = true
            activity.isEligibleForSearch = false
            activity.isEligibleForPublicIndexing = false
            activity.webpageURL = CavnarHandoff.webpageURL(for: navPath)
        }
    }
}
