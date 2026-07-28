import Foundation

/// Where the app talks to. Debug builds (Simulator against a local `python3
/// hosted_dashboard.py`) hit localhost; Release builds hit the real
/// deployment. Swap CAVNAR_API_BASE_URL as an Xcode scheme environment
/// variable to point a Debug build at a different host (e.g. a LAN IP, for
/// testing from a physical device against the same Mac) without editing code.
enum AppEnvironment {
    static var baseURL: URL {
        if let override = ProcessInfo.processInfo.environment["CAVNAR_API_BASE_URL"],
           let url = URL(string: override) {
            return url
        }
        #if DEBUG
        return URL(string: "http://localhost:5000")!
        #else
        return URL(string: "https://dashboard.cavnar.ai")!
        #endif
    }
}
