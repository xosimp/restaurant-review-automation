import SwiftUI

/// Re-fetches when the app returns to the foreground, but only if what's on
/// screen is older than `maxAge`.
///
/// Only LaborView did this before, so Home, Reviews, Intel and Marketing
/// showed hours-old numbers after a backgrounded app was reopened, with no
/// indication they were stale (audit 4.2). RootView deliberately hoists
/// homeViewModel above the lock-screen swap to avoid a reload flash on every
/// unlock — that decision is right for perceived speed, it just needed a
/// freshness policy alongside it, which is what the age check provides.
struct RefreshOnForeground: ViewModifier {
    let maxAge: TimeInterval
    let lastLoaded: Date?
    let reload: () async -> Void
    @Environment(\.scenePhase) private var scenePhase

    func body(content: Content) -> some View {
        content.onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            // No timestamp means nothing has successfully loaded yet — the
            // view's own .task owns that case.
            guard let lastLoaded, Date().timeIntervalSince(lastLoaded) > maxAge else { return }
            Task { await reload() }
        }
    }
}

extension View {
    func refreshOnForeground(
        olderThan maxAge: TimeInterval = 300,
        lastLoaded: Date?,
        reload: @escaping () async -> Void
    ) -> some View {
        modifier(RefreshOnForeground(maxAge: maxAge, lastLoaded: lastLoaded, reload: reload))
    }
}
