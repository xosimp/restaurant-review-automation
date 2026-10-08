import SwiftUI
import WidgetKit

/// The widget extension (Friction audit #47): the "what's waiting · last
/// night" widget for the Home Screen and Lock Screen, the labor % and food
/// cost % widget (parity audit #59), the staff "Next shift" widget, the
/// Live Activities — the auto-publish countdown, "Building next week" (#38)
/// and "Tonight's service" (#94) — and, on iOS 18, the Control Center
/// buttons (#97). It never signs in and never calls the API — the app writes
/// WidgetSnapshot into the shared app group and starts or registers the
/// activities; this target only draws them. Every view here is drawn dark
/// (#18).
@main
struct CavnarWidgetsBundle: WidgetBundle {
    var body: some Widget {
        CavnarWaitingWidget()
        CavnarLastNightWidget()
        CavnarCostsWidget()
        // The staff tier's: the employee's own next shift (MISS-11).
        CavnarNextShiftWidget()
        PendingSendLiveActivity()
        ScheduleBuildLiveActivity()
        ServiceLiveActivity()
        if #available(iOS 18.0, *) {
            CavnarScanInvoiceControl()
            CavnarAskControl()
        }
    }
}
