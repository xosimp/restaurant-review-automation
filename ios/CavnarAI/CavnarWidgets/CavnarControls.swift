import AppIntents
import SwiftUI
import WidgetKit

/// Control Center, the Lock Screen and the Action button (parity audit #97,
/// iOS 18): "Scan invoice" and "Ask Cavnar AI". Each only OPENS the app at
/// the place — the same links the Home Screen quick actions use — and never
/// sends, posts or approves anything (the rule in CavnarAppIntents.swift):
/// a scanned invoice still waits for the owner to apply it, and Ask opens
/// with an empty question box.
@available(iOS 18.0, *)
struct CavnarScanInvoiceControl: ControlWidget {
    static let kind = "ai.cavnar.control.scan-invoice"

    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: Self.kind) {
            ControlWidgetButton(action: OpenURLIntent(URL(string: "cavnarai://nav/inventory/invoices?scan=camera")!)) {
                Label("Scan invoice", systemImage: "doc.text.viewfinder")
            }
        }
        .displayName("Scan invoice")
        .description("Opens the camera in Cavnar AI to read a supplier invoice.")
    }
}

@available(iOS 18.0, *)
struct CavnarAskControl: ControlWidget {
    static let kind = "ai.cavnar.control.ask"

    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: Self.kind) {
            ControlWidgetButton(action: OpenURLIntent(URL(string: "cavnarai://nav/ask")!)) {
                Label("Ask Cavnar AI", systemImage: "sparkles")
            }
        }
        .displayName("Ask Cavnar AI")
        .description("Opens Ask Cavnar AI.")
    }
}
