import AppIntents
import Foundation
import WidgetKit

/// One of the owner's locations, for Siri, Shortcuts and the widget's
/// "Location" setting (parity audit #96). The list is the app's last read
/// of /mobile/api/group-locations (WidgetLocations): in the widget
/// extension — which never signs in — that stored list is all there is; in
/// the app the query reads the route first, so Shortcuts sees a fresh list.
struct CavnarLocationEntity: AppEntity, Identifiable {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Location"
    static let defaultQuery = CavnarLocationQuery()

    let id: Int
    let name: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(name)")
    }

    init(id: Int, name: String) {
        self.id = id
        self.name = name
    }

    init(_ option: WidgetLocationOption) {
        self.init(id: option.id, name: option.name)
    }
}

struct CavnarLocationQuery: EntityQuery {
    init() {}

    func entities(for identifiers: [Int]) async throws -> [CavnarLocationEntity] {
        let all = await Self.options()
        return identifiers.compactMap { id in all.first { $0.id == id }.map(CavnarLocationEntity.init) }
    }

    func suggestedEntities() async throws -> [CavnarLocationEntity] {
        await Self.options().map(CavnarLocationEntity.init)
    }

    /// The app's own read when it can make one; the stored list otherwise.
    static func options() async -> [WidgetLocationOption] {
        #if CAVNAR_WIDGET_EXTENSION
        return WidgetLocations.load()
        #else
        if let fresh = await WidgetLocationsReader.fetch() { return fresh }
        return WidgetLocations.load()
        #endif
    }
}

/// The location a Home or Lock Screen widget draws (#96). Nothing set is
/// the location the app is on — the widget's behaviour before it had a
/// setting, so a widget already on the Home Screen keeps drawing what it did.
struct CavnarLocationWidgetIntent: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "Location"
    static let description = IntentDescription("Which of your locations this widget shows.")

    @Parameter(title: "Location")
    var location: CavnarLocationEntity?

    init() {}
}

/// Which cost the labor / food cost widget's Lock Screen gauge shows (#59).
enum CavnarCostMetric: String, AppEnum {
    case labor
    case food

    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Cost"
    static let caseDisplayRepresentations: [CavnarCostMetric: DisplayRepresentation] = [
        .labor: "Labor %",
        .food: "Food cost %",
    ]
}

struct CavnarCostsWidgetIntent: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "Labor and food cost"
    static let description = IntentDescription("Last night's labor % and food cost % against their targets.")

    @Parameter(title: "Location")
    var location: CavnarLocationEntity?

    @Parameter(title: "Gauge shows", default: .labor)
    var metric: CavnarCostMetric

    init() {}
}
