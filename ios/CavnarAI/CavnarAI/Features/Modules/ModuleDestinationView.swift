import SwiftUI

/// Central mapping from a module key to its actual screen — used by both
/// Home's KPI grid tiles and the Modules tab's grid, so there's one place
/// to update as new module screens are built, not two switch statements
/// drifting apart. An unrecognized/coming_soon key falls through to
/// ComingSoonView rather than crashing or showing nothing.
///
/// The route's focus (friction audit #3) reaches the screens that use it:
/// Reviews opens on the filter or the review, Labor with the section open.
/// Every module screen carries the bell, and — for an owner with more than
/// one location — the location's name under its title (#32).
struct ModuleDestinationView: View {
    let moduleKey: String
    let moduleLabel: String
    var route: ModuleRoute? = nil

    init(moduleKey: String, moduleLabel: String) {
        self.moduleKey = moduleKey
        self.moduleLabel = moduleLabel
    }

    init(route: ModuleRoute) {
        self.moduleKey = route.key
        self.moduleLabel = route.label
        self.route = route
    }

    var body: some View {
        screen
            .environment(\.cavnarShowsLocationTitle, true)
            .toolbar {
                cavnarToolbarItem(placement: .topBarTrailing) { CavnarBellButton() }
            }
    }

    @ViewBuilder
    private var screen: some View {
        switch moduleKey {
        case "reviews":
            ReviewsListView(initialFilter: route?.filter, focusReviewId: route?.itemId.flatMap { Int($0) })
        case "inventory":
            // A counts-only login (the tile's mode) gets the stock work and
            // nothing that reads the margins — the web's Counts tab.
            if ModuleAccess.shared.isCountsOnly("inventory") {
                FoodCostCountsOnlyView(focus: route?.navPath)
            } else {
                FoodCostQuickEntryView(focus: route?.navPath)
            }
        case "labor":
            LaborView(focusSection: route?.section, focusItem: route?.itemId)
        case "marketing":
            MarketingView()
        case "intel":
            IntelView()
        default:
            ComingSoonView(moduleLabel: moduleLabel)
        }
    }
}
