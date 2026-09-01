import SwiftUI
import UIKit

@main
struct CavnarAIApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var sessionStore = SessionStore()

    init() {
        // Every .refreshable in the app draws its own ember drop instead of
        // the system spinner (see CavnarEmberRefreshable) — the UIRefreshControl
        // SwiftUI uses underneath still owns the gesture, its spinner is just
        // tinted clear so only the ember shows.
        UIRefreshControl.appearance().tintColor = .clear
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(sessionStore)
        }
    }
}
