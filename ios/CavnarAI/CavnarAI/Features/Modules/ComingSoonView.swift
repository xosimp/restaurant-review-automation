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
            Text("\(moduleLabel) is coming to the app soon")
                .font(.cavnarHeadline(20))
                .foregroundStyle(Color.cavnarInk)
                .multilineTextAlignment(.center)
            Text("This is available on desktop today — mobile support is on the way.")
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
