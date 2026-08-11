import SwiftUI
import UIKit

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
            // .sensoryFeedback instead of manually firing a generator off
            // onChange(of: isPressed) — the manual version raced SwiftUI's
            // own gesture recognition on fast/rapid taps (reported as
            // missing or delayed feedback); this is Apple's own dedicated,
            // race-resistant mechanism for tying haptics to a value change.
            .sensoryFeedback(.impact(weight: .light), trigger: configuration.isPressed) { old, new in
                new && !isDisabled
            }
    }
}

/// Outlined counterpart to CavnarPrimaryButtonStyle — same ember family,
/// unfilled, for a secondary action sitting next to (or under) a primary
/// one, e.g. Cancel under Send. Deliberately a plain border/fill, not
/// .glassEffect(...interactive()) — see platformCard's fix in Reviews
/// Analytics for why an interactive-glass secondary action next to a plain
/// gesture-driven primary one is worth avoiding here.
struct CavnarSecondaryButtonStyle: ButtonStyle {
    var isDisabled: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.cavnarBody(16, weight: 600))
            .frame(maxWidth: .infinity)
            .padding(14)
            .foregroundStyle(isDisabled ? Color.cavnarEmber.opacity(0.4) : Color.cavnarEmber)
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(isDisabled ? Color.cavnarEmber.opacity(0.35) : Color.cavnarEmber, lineWidth: 1.5)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
            .sensoryFeedback(.impact(weight: .light), trigger: configuration.isPressed) { old, new in
                new && !isDisabled
            }
    }
}

/// Same glass treatment as CavnarSegmentedControl's segments — real Liquid
/// Glass (`.glassEffect`) on iOS 26, Material+ember fallback below it — so
/// paired actions like Skip/Approve read as part of the same visual family
/// as the tab switcher instead of introducing a third button language.
/// isProminent picks which segment state to mirror: the tinted "selected"
/// look for the primary action, the plain "unselected" glass for secondary.
struct CavnarGlassButtonStyle: ButtonStyle {
    var isProminent: Bool = true
    var isDisabled: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        let content = configuration.label
            .font(.cavnarBody(15, weight: 600))
            .foregroundStyle(isProminent ? Color.cavnarInk : Color.cavnarInk2)
            .frame(maxWidth: .infinity)
            .padding(14)

        Group {
            if #available(iOS 26.0, *) {
                content.glassEffect(
                    isProminent ? .regular.tint(Color.cavnarEmber.opacity(0.85)).interactive()
                                : .regular.interactive(),
                    in: RoundedRectangle(cornerRadius: CavnarRadius.control)
                )
            } else {
                content
                    .background {
                        RoundedRectangle(cornerRadius: CavnarRadius.control).fill(.ultraThinMaterial)
                        RoundedRectangle(cornerRadius: CavnarRadius.control)
                            .fill(Color.cavnarEmber.opacity(isProminent ? 0.55 : 0.08))
                        if isProminent {
                            RoundedRectangle(cornerRadius: CavnarRadius.control).fill(
                                LinearGradient(
                                    colors: [Color.white.opacity(0.10), Color.white.opacity(0)],
                                    startPoint: .top, endPoint: .center
                                )
                            )
                        }
                    }
                    .shadow(color: .black.opacity(isProminent ? 0.2 : 0), radius: 3, y: 1)
            }
        }
        .opacity(isDisabled ? 0.4 : 1)
        .scaleEffect(configuration.isPressed ? 0.98 : 1)
        .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
        .sensoryFeedback(.impact(weight: .light), trigger: configuration.isPressed) { old, new in
            new && !isDisabled
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

/// House loading-state convention — a sliding ember highlight sweeping
/// across a dim base bar, instead of a spinner or "...". Sized by its
/// parent (GeometryReader fills whatever width/height it's given), so drop
/// it into any layout as a stand-in for the content that isn't back yet.
struct CavnarSkeletonBar: View {
    var height: CGFloat = 12
    var widthFraction: CGFloat = 1.0

    @State private var slide = false

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Color.cavnarEmber.opacity(0.12)
                LinearGradient(
                    colors: [.clear, Color.cavnarEmber.opacity(0.65), .clear],
                    startPoint: .leading, endPoint: .trailing
                )
                .frame(width: geo.size.width * widthFraction * 0.5)
                .offset(x: slide ? geo.size.width * widthFraction : -geo.size.width * widthFraction * 0.5)
            }
            .frame(width: geo.size.width * widthFraction, height: height)
            .clipShape(RoundedRectangle(cornerRadius: max(3, height / 3)))
        }
        .frame(height: height)
        .onAppear {
            withAnimation(.linear(duration: 1.4).repeatForever(autoreverses: false)) {
                slide = true
            }
        }
    }
}

/// A stack of CavnarSkeletonBar lines with varying widths, mimicking a
/// paragraph of text still loading (used for the draft box, AI insight
/// rows, etc.) — each line slides independently since they all
/// .onAppear-trigger their own animation.
struct CavnarSkeletonLines: View {
    var widths: [CGFloat] = [1.0, 0.86, 0.55]
    var lineHeight: CGFloat = 12
    var spacing: CGFloat = 10

    var body: some View {
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(widths.indices, id: \.self) { i in
                CavnarSkeletonBar(height: lineHeight, widthFraction: widths[i])
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
                    colors: [tint.opacity(0.26), tint.opacity(0.07)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                )
            )
            .overlay(alignment: .top) {
                Rectangle()
                    .fill(tint.opacity(0.55))
                    .frame(height: 1)
            }
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(tint.opacity(0.35), lineWidth: 1)
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

/// A dark branded-ember wash from the very top of the screen, fading into
/// the flat jet-black content background — applied only to the module
/// screens (Reviews/Labor/Food Cost/Marketing/Intel), not Home/Modules/
/// Account, to give each module a bit of distinct identity without
/// touching the app's global black baseline. .ignoresSafeArea() so the
/// gradient actually starts behind the status bar/nav bar, not below it.

/// A circle with a fully transparent fill and only an ember stroke — no
/// material, no tint fill, nothing opaque — so the module gradient bleeds
/// straight through the ring where the system's own chrome allows it.
struct CavnarOutlineCircle: View {
    var body: some View {
        Circle()
            .fill(Color.clear)
            .overlay(Circle().strokeBorder(Color.cavnarEmber.opacity(0.45), lineWidth: 1))
    }
}

/// Ember-colored centered nav title, overriding the app-wide cream title
/// color (set globally via UINavigationBarAppearance in AppDelegate) —
/// SwiftUI's .navigationTitle has no per-screen color modifier, so this
/// swaps in a styled principal toolbar item instead.
extension View {
    func cavnarEmberTitle(_ title: String) -> some View {
        toolbar {
            ToolbarItem(placement: .principal) {
                Text(title)
                    .font(.cavnarBody(17, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
            }
        }
    }
}

/// Ember-colored back chevron via a real ToolbarItem — NOT via hiding the
/// system nav bar. A `.toolbar(.hidden, for: .navigationBar)` + custom
/// safeAreaInset header was tried here and broke real navigation (both the
/// button and the interactive swipe-back gesture stopped working — hiding
/// the bar that way disconnects UINavigationController's pop gesture from
/// the view, a known UIKit/SwiftUI interaction, not just a visual change).
/// Reverted to this: the back button still shows a faint system-drawn
/// circle behind it on iOS 26 that .buttonStyle(.plain) can't fully strip
/// (confirmed by trial), but navigation actually working takes priority
/// over that cosmetic imperfection.
/// `.navigationBarBackButtonHidden(true)` also disables
/// `UINavigationController`'s interactive edge-swipe-to-pop gesture as a
/// side effect (and can leave the leading edge in a state where the first
/// tap on our own back button only "arms" it instead of firing) — this
/// silently re-enables that gesture recognizer with a fresh default
/// delegate underneath our custom button, so hiding the system back button
/// for the ember-chevron restyle doesn't cost us swipe-back or reliable
/// single-tap.
private struct InteractivePopGestureEnabler: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> UIViewController {
        let controller = UIViewController()
        DispatchQueue.main.async {
            guard let nav = controller.navigationController else { return }
            nav.interactivePopGestureRecognizer?.isEnabled = true
            nav.interactivePopGestureRecognizer?.delegate = nil
        }
        return controller
    }

    func updateUIViewController(_ uiViewController: UIViewController, context: Context) {}
}

private struct CavnarEmberBackButton: ViewModifier {
    @Environment(\.dismiss) private var dismiss

    func body(content: Content) -> some View {
        content
            .navigationBarBackButtonHidden(true)
            .background(InteractivePopGestureEnabler().frame(width: 0, height: 0))
            .toolbar {
                ToolbarItem(placement: .navigation) {
                    Button {
                        dismiss()
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                    .buttonStyle(.plain)
                }
            }
    }
}

extension View {
    func cavnarEmberBackButton() -> some View {
        modifier(CavnarEmberBackButton())
    }
}

extension View {
    func cavnarModuleBackground() -> some View {
        background(
            ZStack(alignment: .top) {
                Color.cavnarPaper
                LinearGradient(
                    colors: [Color.cavnarEmber.opacity(0.38), Color.cavnarEmber.opacity(0)],
                    startPoint: .top, endPoint: .bottom
                )
                .frame(height: 340)
            }
            .ignoresSafeArea()
        )
    }
}
