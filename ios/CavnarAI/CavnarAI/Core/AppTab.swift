import Foundation

/// The app's top-level tabs — mirrors the subset of the web dashboard's tabs
/// that make up the v1 mobile scope (Home, Reviews, Ask Cavnar, Food Cost).
/// Labor/Marketing/Intel are desktop-first workflows deferred to v2/v3 (see
/// the architecture plan) and have no tab here yet.
enum AppTab: String, CaseIterable, Identifiable {
    case home
    case reviews
    case askCavnar
    case foodCost

    var id: String { rawValue }

    var title: String {
        switch self {
        case .home: return "Home"
        case .reviews: return "Reviews"
        case .askCavnar: return "Ask Cavnar"
        case .foodCost: return "Food Cost"
        }
    }

    var systemImage: String {
        switch self {
        case .home: return "house.fill"
        case .reviews: return "star.bubble.fill"
        case .askCavnar: return "sparkles"
        case .foodCost: return "dollarsign.circle.fill"
        }
    }
}
