import SwiftUI

/// Privacy mode (Security -> This device): the big dollar figures and KPI
/// numbers blur behind an ember-dotted veil until tapped, then show for a
/// few seconds and hide again. For the app sitting open on the pass, or
/// handed to a server to check something — the app-switcher shield only
/// covers the switcher, this covers the room.
private struct CavnarSensitive: ViewModifier {
    @State private var prefs = AppPreferences.shared
    @State private var revealedUntil: Date = .distantPast
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            let masked = prefs.privacyMode && Date() >= revealedUntil
            content
                .blur(radius: masked ? 9 : 0)
                .opacity(masked ? 0.55 : 1)
                .overlay {
                    if masked {
                        HStack(spacing: 5) {
                            ForEach(0..<4, id: \.self) { _ in
                                Circle().fill(Color.cavnarEmber2).frame(width: 6, height: 6)
                            }
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.cavnarPaper.opacity(0.55), in: Capsule())
                        .transition(.opacity)
                    }
                }
                .contentShape(Rectangle())
                .onTapGesture {
                    guard prefs.privacyMode else { return }
                    Haptic.light()
                    revealedUntil = Date().addingTimeInterval(8)
                }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.25), value: masked)
                .accessibilityLabel(masked ? "Hidden — tap to reveal" : "")
        }
    }
}

extension View {
    /// Mark a figure as sensitive — honoured only while privacy mode is on.
    func cavnarSensitive() -> some View { modifier(CavnarSensitive()) }
}
