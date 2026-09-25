import SwiftUI
import WidgetKit

/// The widget extension (Friction audit #47): the "what's waiting · last
/// night" widget for the Home Screen and Lock Screen, and the auto-publish
/// countdown Live Activity. It never signs in and never calls the API — the
/// app writes WidgetSnapshot into the shared app group and starts the
/// activity; this target only draws them.
@main
struct CavnarWidgetsBundle: WidgetBundle {
    var body: some Widget {
        CavnarWaitingWidget()
        PendingSendLiveActivity()
    }
}
