import Foundation

/// The app's top-level tabs. Ask Cavnar moved to a persistent floating
/// action button (matching the web app's own FAB pattern) instead of a tab,
/// and Reviews/Food Cost/Labor/Marketing/Intel all live inside the Modules
/// tab's grid rather than as separate tabs — a design that scales to any
/// module count (see the architecture plan) instead of needing a redesign
/// every time a module ships.
enum AppTab: String, CaseIterable, Identifiable {
    case home
    case modules
    // Ask Cavnar is a tab now, not a floating button over every screen —
    // it's the app's assistant, and it earned a permanent seat between
    // Modules and Account (the FAB sat over tap targets and its sheet
    // fought the keyboard for gestures).
    case ask
    case account

    var id: String { rawValue }

    var title: String {
        switch self {
        case .home: return "Home"
        case .modules: return "Modules"
        case .ask: return "Ask Cavnar"
        case .account: return "Account"
        }
    }

    var systemImage: String {
        switch self {
        case .home: return "house.fill"
        case .modules: return "square.grid.2x2.fill"
        case .ask: return "sparkles"
        case .account: return "person.crop.circle"
        }
    }
}
