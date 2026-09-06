import SwiftUI

/// Branded loading state for a screen's first, full-screen data fetch.
///
/// Was the seal mark (ring + ember) on a breathing pulse. It's the orb
/// now — specifically the `connecting` state, the drifting node web with
/// signals travelling between nodes. Every one of this view's call sites
/// is a screen waiting on the network, and that is exactly what
/// `connecting` depicts, so the picture finally says something true about
/// what the app is doing instead of just pulsing a logo. It's also the
/// same state Ask Cavnar shows the moment a request goes out, which makes
/// one visual language for "the app is talking to the server" across the
/// whole app rather than two unrelated ones.
///
/// The other eight states were considered and rejected for this job:
/// `searching`/`solving`/`working` all claim the SERVER is reasoning,
/// which is a lie for a plain GET; `breathing` is the idle state and
/// reads as "nothing is happening"; the rest are tied to specific Ask
/// Cavnar moments and would dilute their meaning.
///
/// The launch splash and the Face ID lock screen deliberately still draw
/// the real seal (CavnarSealMark) — those are the brand lockup, not a
/// loading state, and they are the strongest brand moments in the app.
struct CavnarLoadingOrb: View {
    /// 56 rather than the seal's old 40: this is usually the only thing on
    /// an otherwise empty screen, and it resolves to the orb's 64pt preset
    /// tuning (see CavnarOrbEngine.resolvePreset), which is the density
    /// the web state was actually tuned at.
    var size: CGFloat = 56

    var body: some View {
        CavnarOrb(state: .connecting, size: size)
    }
}
