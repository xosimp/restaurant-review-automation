import Foundation

/// Maps the backend's semantic module-icon keys (models.get_active_modules(),
/// see HomeSummary.swift's ModuleSummary) to actual SF Symbols. Kept as its
/// own lookup — rather than a literal symbol name from the backend — so a
/// brand-new module can ship server-side and show up in the Home grid and
/// Modules tab immediately, with a sensible fallback icon, before iOS has
/// been updated to recognize its specific key.
enum ModuleIcon {
    static func symbolName(for key: String) -> String {
        switch key {
        case "reviews": return "star.bubble.fill"
        case "labor": return "person.2.fill"
        case "inventory", "food-cost": return "dollarsign.circle.fill"
        case "marketing": return "megaphone.fill"
        case "intel": return "binoculars.fill"
        case "waitlist": return "person.crop.circle.badge.clock"
        case "bar": return "wineglass.fill"
        default: return "square.grid.2x2"
        }
    }
}
