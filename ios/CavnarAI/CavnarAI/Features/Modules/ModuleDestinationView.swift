import SwiftUI

/// Central mapping from a module key to its actual screen — used by both
/// Home's KPI grid tiles and the Modules tab's grid, so there's one place
/// to update as new module screens are built, not two switch statements
/// drifting apart. An unrecognized/coming_soon key falls through to
/// ComingSoonView rather than crashing or showing nothing.
struct ModuleDestinationView: View {
    let moduleKey: String
    let moduleLabel: String

    var body: some View {
        switch moduleKey {
        case "reviews":
            ReviewsListView()
        case "inventory":
            FoodCostQuickEntryView()
        case "labor":
            LaborView()
        case "marketing":
            MarketingView()
        case "intel":
            IntelView()
        default:
            ComingSoonView(moduleLabel: moduleLabel)
        }
    }
}
