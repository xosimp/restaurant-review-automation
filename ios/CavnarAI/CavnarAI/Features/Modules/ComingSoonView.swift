import SwiftUI

/// Destination for a module a client is entitled to but that doesn't have a
/// real iOS screen yet (reserved for genuinely future modules — Waitlist,
/// Bar & Alcohol — not used by anything shipping today; see the
/// architecture plan). Keeps the Modules grid always accurate without ever
/// hiding what a client is actually paying for.
struct ComingSoonView: View {
    let moduleLabel: String

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "hourglass")
                .font(.system(size: 36))
                .foregroundStyle(Color.cavnarEmber)
            Text("\(moduleLabel) is coming soon")
                .font(.cavnarHeadline(20))
                .foregroundStyle(Color.cavnarInk)
                .multilineTextAlignment(.center)
            // Not "available on desktop today": the registry's coming-soon
            // modules (models._MODULE_REGISTRY — Waitlist, Bar & Alcohol)
            // are not built anywhere yet, web included.
            Text("It isn\u{2019}t built yet, on the web or in the app. It will open here when it\u{2019}s ready.")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.cavnarPaper)
        .navigationTitle(moduleLabel)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar(moduleLabel) }
    }
}
