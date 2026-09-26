import Foundation
import Observation

/// How this login may open each module, from the tiles the server sent
/// (mobile_api._module_tile_mode): "full", or "counts" for Food Cost's
/// stock work without a dollar figure (FOOD_COST_ENTER without
/// FOOD_COST_VIEW). Recorded wherever the app decodes the tiles — Home and
/// the Modules tab — so a module screen reached from any route, a push
/// included, opens in the mode the login actually has. A screen that is
/// refused (403 module_forbidden) before any tiles arrived records it too.
@Observable
@MainActor
final class ModuleAccess {
    static let shared = ModuleAccess()

    /// Module keys this login opens counts-only.
    private(set) var countsOnly: Set<String> = []

    func record(_ modules: [ModuleSummary]) {
        countsOnly = Set(modules.filter { $0.mode == "counts" }.map(\.key))
    }

    /// A refusal from the server is the same answer the tile would give.
    func markCountsOnly(_ key: String) {
        countsOnly.insert(key)
    }

    func isCountsOnly(_ key: String) -> Bool { countsOnly.contains(key) }
}
