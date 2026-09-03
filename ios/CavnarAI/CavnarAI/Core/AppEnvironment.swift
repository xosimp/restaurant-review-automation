import Foundation

/// Where the app talks to. Debug builds (Simulator against a local `python3
/// hosted_dashboard.py`) hit localhost; Release builds hit the real
/// deployment. Export CAVNAR_DEV_API_BASE_URL in your shell to point a Debug
/// build at a different host (a LAN IP or an ngrok tunnel, for testing from a
/// physical device) — xcodegen substitutes it into the scheme at generate
/// time, so it survives `xcodegen generate` without the address being
/// committed. See project.yml's environmentVariables comment.
enum AppEnvironment {
    static var baseURL: URL {
        if let url = validatedOverride() { return url }
        #if DEBUG
        return URL(string: "http://localhost:5000")!
        #else
        return URL(string: "https://dashboard.cavnar.ai")!
        #endif
    }

    /// The override is externally supplied, so it is the one URL in the app
    /// that cannot be trusted to be well-formed. Three things are rejected
    /// rather than force-unwrapped or passed through blindly:
    ///
    /// 1. An unsubstituted `${CAVNAR_DEV_API_BASE_URL}` placeholder, which is
    ///    exactly what the scheme carries when the variable isn't exported.
    /// 2. Anything that isn't http/https — a custom scheme here would send
    ///    every bearer-token request somewhere unexpected.
    /// 3. Anything with no host.
    ///
    /// Any of those falls back to the compiled default instead of crashing
    /// (the old force-unwrap path) or silently misrouting traffic.
    private static func validatedOverride() -> URL? {
        guard let raw = ProcessInfo.processInfo.environment["CAVNAR_API_BASE_URL"]?
            .trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty,
              !raw.hasPrefix("${"),
              let url = URL(string: raw),
              let scheme = url.scheme?.lowercased(),
              scheme == "https" || scheme == "http",
              url.host?.isEmpty == false
        else { return nil }
        return url
    }

    /// True when this build is talking to the real deployment — the only case
    /// where certificate pinning applies (a local server or a dev tunnel
    /// legitimately presents a different chain). See PinnedSessionDelegate.
    static var isProductionHost: Bool {
        baseURL.host?.hasSuffix("cavnar.ai") == true
    }
}
