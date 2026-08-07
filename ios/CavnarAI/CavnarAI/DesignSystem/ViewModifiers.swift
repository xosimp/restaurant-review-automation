import SwiftUI

/// One consistent corner-radius scale for every rounded element in the app —
/// buttons, fields, cards, and sheets all pull from here instead of each
/// call site inventing its own value. Nothing in the app should render with
/// square corners; this is what keeps "rounded" uniform end to end.
enum CavnarRadius {
    static let control: CGFloat = 12   // buttons, text fields, small chips
    static let card: CGFloat = 16      // cards, tiles, grouped content
    static let sheet: CGFloat = 24     // sheets, modals, large surfaces
    static let pill: CGFloat = 999     // fully-rounded badges/capsules
}

/// Shared field/button chrome so every screen doesn't hand-roll the same
/// padding/corner-radius/background values.
struct CavnarTextFieldStyle: ViewModifier {
    func body(content: Content) -> some View {
        content
            .font(.cavnarBody(15))
            .padding(14)
            .background(Color.cavnarPaper2)
            .foregroundStyle(Color.cavnarInk)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}

extension View {
    func cavnarTextFieldStyle() -> some View {
        modifier(CavnarTextFieldStyle())
    }
}

struct CavnarPrimaryButtonStyle: ButtonStyle {
    var isDisabled: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.cavnarBody(16, weight: 600))
            .frame(maxWidth: .infinity)
            .padding(14)
            .background(isDisabled ? Color.cavnarEmber.opacity(0.4) : Color.cavnarEmber)
            .foregroundStyle(Color.cavnarInk)
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, isPressed in
                if isPressed && !isDisabled { Haptic.light() }
            }
    }
}

/// Deliberately light — a faint tint fill plus a hairline border communicates
/// grouping without the flat, blocky "everything is a solid box" look. Mirrors
/// how Raycast/Apple's HIG signal elevation in dark mode: a subtle border
/// instead of a heavy filled card.
struct CavnarCardStyle: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(16)
            .background(Color.cavnarPaper2.opacity(0.6))
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}

extension View {
    func cavnarCard() -> some View {
        modifier(CavnarCardStyle())
    }
}

/// Subtle emboss + colored glow on stat numbers — ports the web dashboard's
/// dark-mode .stat-glow-*/.rv-stat-n text-shadow treatment (a soft dark
/// shadow for depth, plus a faint tinted glow) so numbers read as slightly
/// luminous against the near-black background instead of flat.
struct CavnarNumberGlow: ViewModifier {
    var tint: Color = .cavnarEmber

    func body(content: Content) -> some View {
        content
            .shadow(color: .black.opacity(0.35), radius: 0.5, x: 0, y: 1)
            .shadow(color: tint.opacity(0.35), radius: 6, x: 0, y: 0)
    }
}

extension View {
    func cavnarNumberGlow(_ tint: Color = .cavnarEmber) -> some View {
        modifier(CavnarNumberGlow(tint: tint))
    }
}

/// Gradient-tinted stat cell — ports the web dashboard's .rv-stat-cell: a
/// diagonal brand-color wash, translucent border, and a 1px top highlight
/// line, instead of a flat solid card fill. Use for hero/primary stat
/// displays where a plain .cavnarCard() would read too flat.
struct CavnarStatCellStyle: ViewModifier {
    var tint: Color = .cavnarEmber

    func body(content: Content) -> some View {
        content
            .padding(16)
            .background(
                LinearGradient(
                    colors: [tint.opacity(0.16), tint.opacity(0.04)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                )
            )
            .overlay(alignment: .top) {
                Rectangle()
                    .fill(tint.opacity(0.4))
                    .frame(height: 1)
            }
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(tint.opacity(0.25), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}

extension View {
    func cavnarStatCell(tint: Color = .cavnarEmber) -> some View {
        modifier(CavnarStatCellStyle(tint: tint))
    }
}

/// Frosted-glass surface for sheets/overlays via native Material — the
/// glassmorphism baseline every major platform has converged on (iOS 26
/// Liquid Glass, Spotify's frosted panels) rather than a flat opaque sheet.
extension View {
    func cavnarGlassBackground(_ material: Material = .ultraThinMaterial) -> some View {
        background(material, in: RoundedRectangle(cornerRadius: CavnarRadius.sheet))
    }
}
