import SwiftUI

/// "Showing data from 2h ago" — Home's staleness line (HomeView, audit 6.5),
/// for every screen that paints from the device cache (ResponseCache): the
/// same 12.5pt amber sentence with its figures in Space Grotesk, centred.
/// Draws nothing when `text` is nil, so a screen places it unconditionally.
struct CachedDataNotice: View {
    let text: String?

    var body: some View {
        if let text {
            HomeMixedText.make(text, size: 12.5, weight: 600, color: .cavnarAmber)
                .frame(maxWidth: .infinity)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
                .accessibilityAddTraits(.isStaticText)
        }
    }
}
