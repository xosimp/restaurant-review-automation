import Foundation

/// A lightweight, Hashable stand-in for "push to this module's screen" —
/// shared by every screen that navigates into a module (Home's KPI grid,
/// the Modules tab, Home's needs-attention list) via a NavigationPath
/// rather than NavigationLink, so the haptic that accompanies the tap
/// fires from a deterministic Button action closure instead of racing
/// NavigationLink's own gesture recognition.
///
/// `filter`, `section` and `itemId` carry where inside the module the tap
/// was about (friction audit #3, 9/25/26): "Reply now" on urgent reviews
/// opens the inbox on Urgent, a time-off push opens Labor with Time off
/// expanded. A module screen that doesn't know the focus just opens.
struct ModuleRoute: Hashable {
    let key: String
    let label: String
    var filter: String? = nil
    var section: String? = nil
    var itemId: String? = nil

    /// The route a nav path (Core/NavPath.swift) opens inside the Modules
    /// tab, or nil for a head that lives elsewhere (Home, Ask, Account, the
    /// daily report, a pending action). `labelFor` names the module.
    static func from(_ nav: NavPath, labelFor: @escaping (String) -> String = { $0.capitalized }) -> ModuleRoute? {
        func route(_ key: String, filter: String? = nil, section: String? = nil, item: String? = nil) -> ModuleRoute {
            ModuleRoute(key: key, label: labelFor(key), filter: filter, section: section, itemId: item)
        }
        let filter = nav.query["filter"]
        switch nav.head {
        case "reviews":
            return route("reviews", filter: filter, section: nav.target)
        case "review":
            return route("reviews", filter: filter, item: nav.target)
        case "labor":
            return route("labor", section: nav.target)
        case "schedule":
            return route("labor", section: "schedule", item: nav.target)
        case "person":
            return route("labor", section: "team", item: nav.target)
        case "request":
            // request/<kind>-<id>: time off opens its own section, every
            // other kind (a shift hand-back or swap) opens Shift requests.
            let raw = nav.target ?? ""
            let kind = raw.split(separator: "-").dropLast().joined(separator: "-").lowercased()
            let id = raw.split(separator: "-").last.map(String.init)
            let isTimeOff = kind.contains("time") || kind == "pto"
            return route("labor", section: isTimeOff ? "timeoff" : "requests", item: id)
        case "inventory", "food":
            return route("inventory", section: nav.target)
        case "invoice":
            return route("inventory", section: "invoices", item: nav.target)
        case "order":
            return route("inventory", section: "order", item: nav.target)
        case "marketing":
            return route("marketing", section: nav.target)
        case "intel", "competitor":
            return route("intel", section: nav.target)
        default:
            return nil
        }
    }
}
