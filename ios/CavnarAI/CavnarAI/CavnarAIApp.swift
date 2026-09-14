import SwiftUI
import UIKit

@main
struct CavnarAIApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var sessionStore = SessionStore()
    // The employee tier. A second store rather than a mode flag on the first:
    // the two tiers hold different tokens with different lifetimes and reach
    // different APIs, and keeping them as separate objects means there is no
    // state a bug could flip to turn one into the other.
    @State private var staffSessionStore = StaffSessionStore()

    init() {
        #if DEBUG
        DebugFrameWatchdog.start()
        #endif
        // Every .refreshable in the app draws its own ember drop instead of
        // the system spinner (see CavnarEmberRefreshable) — the UIRefreshControl
        // SwiftUI uses underneath still owns the gesture, its spinner is just
        // tinted clear so only the ember shows.
        UIRefreshControl.appearance().tintColor = .clear

        // Screen titles are headings — the same role an ingredient row's own
        // name plays in Food Cost (Clash Display there too, see
        // IngredientCard) — so every navigationTitle across the app ("Labor",
        // "Reviews", "Intel"...) gets the same face, uniformly, from one
        // place instead of a per-screen override. Only title text/color are
        // touched here — background, shadow, and button chrome stay exactly
        // what each screen already configures for itself.
        let titleColor = UIColor(named: "Ink") ?? .white
        if let inlineFont = UIFont(name: "ClashDisplay-Semibold", size: 18) {
            UINavigationBar.appearance().titleTextAttributes = [.font: inlineFont, .foregroundColor: titleColor]
        }
        if let largeFont = UIFont(name: "ClashDisplay-Semibold", size: 34) {
            UINavigationBar.appearance().largeTitleTextAttributes = [.font: largeFont, .foregroundColor: titleColor]
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(sessionStore)
                .environment(staffSessionStore)
        }
    }
}
