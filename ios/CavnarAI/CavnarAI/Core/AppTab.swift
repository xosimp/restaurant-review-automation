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

    var id: String { rawValue }

    var title: String {
        switch self {
        case .home: return "Home"
        case .modules: return "Modules"
        }
    }

    var systemImage: String {
        switch self {
        case .home: return "house.fill"
        case .modules: return "square.grid.2x2.fill"
        }
    }
}
