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

/// The house "branded pill" gradient — full-strength ember on one edge,
/// fading continuously to fully *transparent* on the other, never hard-
/// switching to a solid color partway through (see ScheduleHistoryView's
/// row-gradient fix for why: a solid final stop reads as an abrupt cliff no
/// matter how many stops lead into it). Shared by every "branded pill"
/// surface in the app — Schedule History rows, Food Cost ingredient cards —
/// so they read as one consistent material instead of two separately-tuned
/// gradients that drift apart over time.
enum CavnarEmberFade {
    static let horizontal = LinearGradient(
        stops: [
            .init(color: Color.cavnarEmber.opacity(0.85), location: 0),
            .init(color: Color.cavnarEmber.opacity(0.68), location: 0.28),
            .init(color: Color.cavnarEmber.opacity(0.5), location: 0.48),
            .init(color: Color.cavnarEmber.opacity(0.33), location: 0.64),
            .init(color: Color.cavnarEmber.opacity(0.18), location: 0.78),
            .init(color: Color.cavnarEmber.opacity(0.07), location: 0.9),
            .init(color: Color.cavnarEmber.opacity(0), location: 1),
        ],
        startPoint: .leading, endPoint: .trailing
    )
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

/// The button treatment from the design preview, tuned several times
/// since the first shipped version. Current state: a flat, fully-opaque
/// Ember fill (same color the tab switcher's selected segment uses) —
/// no gradient (was a 3-stop bright-to-deep fade), no glossy white
/// sheen (that read as an unwanted "glass effect"), and no dark
/// vignette either (tried as a replacement for the sheen, but asked to
/// go too); an ember outer border at the true edge plus a brighter
/// Ember2 inner border inset within it; and two perfectly symmetric
/// shadow layers, both centered (x: 0, y: 0) so neither reads as
/// bottom-heavy — a tight, darker elevation shadow applied BEFORE a
/// wider, softer ember glow in the modifier chain. SwiftUI shadows each
/// wrap everything above them, so the shadow applied first stays the
/// crisp layer sitting close to the edge, and the glow applied after it
/// is what extends further out from behind that tight shadow — "shadow
/// in front, glow behind it" was the ask, and this ordering is what
/// actually produces that read. No floor reflection either — it was
/// its own asymmetric glow sitting only below the button, which
/// directly fought an "equal on all sides" requirement. Shared by
/// CavnarPrimaryButtonStyle and CavnarSplitButton.
struct CavnarPremiumButtonSurface: ViewModifier {
    var isDisabled: Bool = false
    // Overridable per call site — CavnarSplitButton's own reference
    // match was a true pill, unlike the moderate rounded-rect default
    // tuned for text CTAs.
    var shape: AnyShape = CavnarPremiumButtonSurface.defaultShape

    static let glowRadius: CGFloat = 20
    static let defaultShape = AnyShape(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))

    func body(content: Content) -> some View {
        content
            .background {
                // Flat, fully-opaque Ember — same color the tab switcher's
                // selected segment uses, with none of that segment's own
                // glass/material or gradient sheen (both tried here in
                // earlier rounds and asked to go). No gradient stops
                // either — this used to be a 3-stop bright-to-deep fade.
                shape.fill(Color.cavnarEmber.opacity(isDisabled ? 0.4 : 1))
            }
            .clipShape(shape)
            // Thin ember border at the true outer edge — the same accent
            // CavnarSegmentedControl's selected tab reads as having (its
            // own real/fallback glass material renders that edge
            // implicitly; this is the explicit equivalent for a plain
            // gradient fill).
            .overlay(shape.stroke(Color.cavnarEmber.opacity(isDisabled ? 0 : 0.7), lineWidth: 1.5))
            // True inner border — inset further within the shape than the
            // outer ember one, not centered on the edge. Ember2 (the
            // brighter/lighter accent), same as the outer border's darker
            // Ember reads on CavnarSegmentedControl — was flat black.
            // AnyShape can't use strokeBorder (needs InsettableShape), so
            // the inset is done by hand: a shape view resizes to fill
            // whatever frame it's given, so padding the stroked view
            // shrinks its rendered geometry by that amount on every side,
            // landing the visible line fully inside the button rather
            // than straddling an edge.
            .overlay(
                shape
                    .stroke(Color.cavnarEmber2.opacity(isDisabled ? 0 : 0.6), lineWidth: 1.5)
                    .padding(1.5)
            )
            .shadow(color: .black.opacity(isDisabled ? 0 : 0.55), radius: 8, x: 0, y: 0)
            .shadow(color: Color.cavnarEmber.opacity(isDisabled ? 0 : 0.4), radius: Self.glowRadius, x: 0, y: 0)
    }
}

extension View {
    func cavnarPremiumButtonSurface(
        isDisabled: Bool = false,
        shape: AnyShape = CavnarPremiumButtonSurface.defaultShape
    ) -> some View {
        modifier(CavnarPremiumButtonSurface(isDisabled: isDisabled, shape: shape))
    }
}

struct CavnarPrimaryButtonStyle: ButtonStyle {
    var isDisabled: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.cavnarBody(16, weight: 600))
            // Was .frame(maxWidth: .infinity) — every primary button
            // stretched to fill its container regardless of how short its
            // label was, which is exactly what read as "way too long
            // horizontally." Buttons now hug their own text; a call site
            // that genuinely wants full width can still add
            // .frame(maxWidth: .infinity) to its own label content.
            .padding(.horizontal, 22)
            .padding(.vertical, 14)
            .foregroundStyle(.white)
            // Flat white read as lifeless against the gradient — a tight,
            // low-opacity drop shadow directly under the glyphs gives the
            // text a slight embossed lift instead of looking pasted flat
            // on top, the same text-shadow the approved reference preview
            // itself used.
            //
            // .compositingGroup() before that shadow matters once the label
            // isn't plain Text: the AI Visibility loading state's label is
            // CavnarShimmerText + CavnarShimmerLine stacked, each itself a
            // masked gradient over a translucent base layer. Without
            // flattening first, SwiftUI's .shadow() draws a shadow PER
            // sublayer rather than one shadow for the whole composited
            // shape — with several overlapping semi-transparent layers that
            // stacks into a diffuse gray haze sitting over the shimmer
            // instead of a clean, barely-there text shadow. compositingGroup
            // renders the label to one flattened layer first so the shadow
            // that follows sees a single opaque silhouette, same as it
            // always did for plain Text.
            .compositingGroup()
            .shadow(color: .black.opacity(0.28), radius: 1, x: 0, y: 1)
            .cavnarPremiumButtonSurface(isDisabled: isDisabled)
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
            .padding(.horizontal, 22)
            .padding(.vertical, 14)
            .foregroundStyle(isDisabled ? Color.cavnarEmber.opacity(0.4) : Color.cavnarEmber)
            .background(
                CavnarPremiumButtonSurface.defaultShape
                    .fill(Color.cavnarEmber.opacity(isDisabled ? 0.03 : 0.09))
            )
            .overlay(
                // A flat single-tone border read thin next to the new
                // gradient-filled primary style — a top-brighter/bottom-
                // dimmer border gives it the same "light hitting an edge"
                // depth cue without filling the button in. Same shape
                // token as the primary style (moderate round, not a full
                // pill) so the two sitting side by side don't disagree.
                CavnarPremiumButtonSurface.defaultShape
                    .stroke(
                        LinearGradient(
                            colors: [
                                Color.cavnarEmber.opacity(isDisabled ? 0.3 : 0.95),
                                Color.cavnarEmber.opacity(isDisabled ? 0.18 : 0.55),
                            ],
                            startPoint: .top, endPoint: .bottom
                        ),
                        lineWidth: 1.5
                    )
            )
            .clipShape(CavnarPremiumButtonSurface.defaultShape)
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
            .sensoryFeedback(.impact(weight: .light), trigger: configuration.isPressed) { old, new in
                new && !isDisabled
            }
    }
}

/// Small solid-color action chip (roadmap items' "Send a review
/// request"/"Go to review queue" style buttons) — was a plain colored
/// Text with no ButtonStyle at all, so it had no press feedback of any
/// kind and no haptic, reading as a colored label rather than something
/// tappable. A colored shadow that recedes on press (rather than a
/// scale-only change) is what actually sells "this is a physical button"
/// at this small a size.
struct CavnarChipButtonStyle: ButtonStyle {
    var tone: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.cavnarBody(11, weight: 600))
            .foregroundStyle(.white)
            .padding(.horizontal, 13)
            .padding(.vertical, 6)
            .background(tone)
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            .shadow(color: tone.opacity(configuration.isPressed ? 0.1 : 0.45), radius: configuration.isPressed ? 1 : 4, x: 0, y: configuration.isPressed ? 0 : 2)
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
            .sensoryFeedback(.impact(weight: .light), trigger: configuration.isPressed) { old, new in
                new
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

/// A deliberately-tinted circular glass background for a lone toolbar
/// icon (back chevron, bell, share, Done) — NOT the automatic system
/// chrome a bare Button gets for free on iOS 26+.
///
/// That automatic wrapping is genuinely translucent material, and this
/// app's screens sit on cavnarModuleBackground()'s own ember wash near
/// the top — an untinted (or app-tint-inherited, which .tint(nil) alone
/// only ever addressed) glass surface shows that wash bleeding through,
/// unevenly, since a gradient doesn't refract symmetrically through a
/// round lens the way a flat color would. That's the "faded glass with
/// an asymmetric orange edge" look — not a stroke or tint this app was
/// drawing, just glass being glass over an orange background.
///
/// The fix is an explicit tint dark/opaque enough to dominate whatever
/// sits behind it (same technique CavnarSegmentedControl's selected
/// segment already uses, just with a neutral color instead of ember),
/// so the glass reads as a predictable dark chip regardless of scroll
/// position or which screen it's on — real Liquid Glass, deliberately
/// controlled, not left to render however the system infers it should.
/// The "Tinted" treatment from the icon-button design preview — a flat,
/// translucent ember wash, no blur/refraction at all. Replaces the
/// previous neutral dark-glass version (real Liquid Glass on iOS 26,
/// material+opaque-paper fallback below it) for every constant-chrome
/// icon button in the app: back, notifications, keyboard nav, share.
/// Deliberately drops real glass entirely rather than trying to tint it
/// ember — the whole "faded glass, asymmetric orange edge" saga
/// documented above this function existed because glass refracts this
/// app's own orange background wash unevenly; a flat color fill has no
/// such failure mode and matches the approved preview exactly. Icons
/// using this should be Color.cavnarEmber themselves (not the previous
/// neutral tint) for the tinted-bg + ember-icon pairing to read right.
extension View {
    func cavnarToolbarIconGlass(size: CGFloat = 34) -> some View {
        self
            .frame(width: size, height: size)
            .background(Color.cavnarEmber.opacity(0.14), in: Circle())
    }
}

/// Same reasoning and tint as cavnarToolbarIconGlass, shaped as a Capsule
/// around its own content's padding instead of a fixed circle — for a
/// text toolbar button (the keyboard's Done key) rather than a lone icon.
extension View {
    func cavnarToolbarPillGlass() -> some View {
        Group {
            if #available(iOS 26.0, *) {
                self
                    .padding(.horizontal, 14)
                    .padding(.vertical, 7)
                    .glassEffect(.regular.tint(Color.cavnarPaper2.opacity(0.92)).interactive(), in: Capsule())
            } else {
                self
                    .padding(.horizontal, 14)
                    .padding(.vertical, 7)
                    .background(.ultraThinMaterial, in: Capsule())
                    .background(Color.cavnarPaper2.opacity(0.75), in: Capsule())
            }
        }
    }
}

/// THE actual fix for every "toolbar icon looks wrong" symptom this app
/// has hit — the oversized/double-wrapped back-button circle, the Done
/// button clipped down to a single "D", the faded-glass-with-asymmetric-
/// orange-edge look before that. All of it traced to one real mechanism,
/// confirmed against Apple's own documentation (not guessed): on iOS 26+,
/// `ToolbarItem`/`ToolbarItemGroup` content is automatically wrapped in a
/// SHARED glass background at the toolbar-group level — a system behavior
/// entirely separate from the inner content's own button style, which is
/// exactly why .buttonStyle(.plain) and .tint(nil) never fully fixed
/// anything: they don't touch this layer at all. That automatic wrapper
/// composited on top of this app's own explicit cavnarToolbarIconGlass()/
/// cavnarToolbarPillGlass() content, doubling up sizing/clipping (the
/// oversized circle, the "D") and, before those existed, showing the
/// page's own background bleeding through unevenly (the orange edge).
///
/// `.sharedBackgroundVisibility(.hidden)` (SwiftUI, iOS 26+, declared on
/// ToolbarContent — https://developer.apple.com/documentation/swiftui/toolbarcontent/sharedbackgroundvisibility(_:))
/// is Apple's own, documented way to opt a toolbar item out of that
/// automatic wrapper. This helper applies it wherever available and
/// no-ops on iOS 17-25 (nothing to hide there — the automatic shared-glass
/// grouping is an iOS 26+ concept), so cavnarToolbarIconGlass()/
/// cavnarToolbarPillGlass() are finally the ONLY chrome being applied,
/// with nothing left compositing on top of them.
@ToolbarContentBuilder
func cavnarToolbarItem<Content: View>(
    placement: ToolbarItemPlacement, @ViewBuilder content: () -> Content
) -> some ToolbarContent {
    if #available(iOS 26.0, *) {
        ToolbarItem(placement: placement, content: content)
            .sharedBackgroundVisibility(.hidden)
    } else {
        ToolbarItem(placement: placement, content: content)
    }
}

/// Same as cavnarToolbarItem, for a ToolbarItemGroup (the keyboard's
/// Done button sits in one alongside a Spacer, not a standalone
/// ToolbarItem).
@ToolbarContentBuilder
func cavnarToolbarItemGroup<Content: View>(
    placement: ToolbarItemPlacement, @ViewBuilder content: () -> Content
) -> some ToolbarContent {
    if #available(iOS 26.0, *) {
        ToolbarItemGroup(placement: placement, content: content)
            .sharedBackgroundVisibility(.hidden)
    } else {
        ToolbarItemGroup(placement: placement, content: content)
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

private struct CavnarWidthKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

private extension View {
    func cavnarReportWidth() -> some View {
        background(
            GeometryReader { geo in
                Color.clear.preference(key: CavnarWidthKey.self, value: geo.size.width)
            }
        )
    }
}

/// A primary action + "Cancel" pair, sized to genuinely match each
/// other (both equal to the wider of the two — in practice always the
/// primary button, since "Cancel" is short) and centered within their
/// container.
///
/// Real width-matching via PreferenceKey, not a frame trick — two prior
/// attempts at "give Cancel .frame(maxWidth: .infinity) and let the
/// VStack cap it at the primary button's width" both failed, and for a
/// real reason: a child requesting infinite width doesn't just expand
/// itself, it makes the WHOLE containing VStack report an unbounded
/// ideal width up to ITS OWN parent — which is why Cancel ended up
/// spanning the full form instead of matching the button above it, not
/// just staying narrow. There's no shortcut around measuring: each
/// button reports its actual rendered width via a hidden GeometryReader
/// or a first pass, the parent takes the max, then re-applies that
/// exact width to both on the next render.
struct CavnarFormButtonPair<Primary: View>: View {
    // cancelLabel comes first (a plain String, not a closure) so both
    // `primary` and `cancelAction` can be used as trailing closures —
    // Swift's multi-trailing-closure syntax needs the closure params
    // grouped at the end, with any non-closure params passed normally
    // in the parens before them:
    //   CavnarFormButtonPair(cancelLabel: "Cancel") { ... } cancelAction: { ... }
    var cancelLabel: String = "Cancel"
    @ViewBuilder var primary: () -> Primary
    let cancelAction: () -> Void

    @State private var matchedWidth: CGFloat?

    var body: some View {
        VStack(spacing: 10) {
            primary()
                .frame(width: matchedWidth)
                .cavnarReportWidth()

            Button(cancelLabel, action: cancelAction)
                .buttonStyle(CavnarSecondaryButtonStyle())
                .frame(width: matchedWidth)
                .cavnarReportWidth()
        }
        .onPreferenceChange(CavnarWidthKey.self) { matchedWidth = $0 }
        .frame(maxWidth: .infinity)
    }
}

/// The app's standing "quick async action, no spinner" convention — a
/// gentle breathing opacity on the loading label instead of a spinner.
/// Same exact curve as AIConsultantView's own private PulsingAnalyzingText
/// and LaborView's PulsingSparkleIcon, pulled out here so any button-style
/// loading state (not just AI-insight text) can reuse it instead of a bare
/// ProgressView().
struct PulsingText: View {
    let text: String
    @State private var pulse = false

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .opacity(pulse ? 1 : 0.45)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) {
                    pulse = true
                }
            }
    }
}

/// A bright band sweeping left-to-right across the text, masked to its
/// own glyph shape — the same shimmer LaborView's own ShimmerText uses
/// for its hero banner's rotating status line, pulled out here as a
/// reusable, single-message version (no message rotation) for anywhere
/// else that wants this specific effect instead of PulsingText's plain
/// opacity breathe. Driven by TimelineView (real wall-clock time)
/// rather than a toggled @State + withAnimation(.repeatForever) — the
/// latter is vulnerable to an ambient parent transaction silently
/// overriding/replacing the repeat-forever animation (documented at
/// length on LaborView's own ShimmerText); computing the sweep position
/// directly from timeline.date sidesteps SwiftUI's animation/transaction
/// system entirely, so nothing from a parent update can interrupt it.
struct CavnarShimmerText: View {
    let text: String
    var color: Color = .white

    private static let period: Double = 1.6

    var body: some View {
        TimelineView(.animation) { timeline in
            let elapsed = timeline.date.timeIntervalSinceReferenceDate
            let phase = (elapsed.truncatingRemainder(dividingBy: Self.period)) / Self.period

            Text(text)
                .foregroundStyle(color.opacity(0.4))
                .overlay(
                    GeometryReader { geo in
                        LinearGradient(
                            colors: [.clear, color, .clear],
                            startPoint: .leading, endPoint: .trailing
                        )
                        .frame(width: geo.size.width * 0.6)
                        .offset(x: -geo.size.width * 0.6 + phase * geo.size.width * 1.6)
                    }
                    .mask(Text(text))
                )
        }
    }
}

/// A thin capsule track with a bright segment sweeping left to right on
/// a loop — the same TimelineView-driven sweep technique as
/// CavnarShimmerText above, just filling a bar instead of masked to
/// text glyphs. For "still working" loading states that want a visible
/// progress-style line under/near a label, not just breathing text.
struct CavnarShimmerLine: View {
    var color: Color = .cavnarEmber
    var height: CGFloat = 3

    private static let period: Double = 1.6

    var body: some View {
        TimelineView(.animation) { timeline in
            let elapsed = timeline.date.timeIntervalSinceReferenceDate
            let phase = (elapsed.truncatingRemainder(dividingBy: Self.period)) / Self.period

            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(color.opacity(0.2))
                    LinearGradient(
                        colors: [.clear, color, .clear],
                        startPoint: .leading, endPoint: .trailing
                    )
                    .frame(width: geo.size.width * 0.5)
                    .offset(x: -geo.size.width * 0.5 + phase * geo.size.width * 1.5)
                }
                .clipShape(Capsule())
            }
            .frame(height: height)
        }
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
            // Only real usage today is the Modules tab's tiles — the
            // original 0.26→0.07 wash read as barely-there against a near-
            // black page, closer to a plain dark card than a branded orange
            // one. Bumped for real presence at a glance.
            .background(
                LinearGradient(
                    colors: [tint.opacity(0.42), tint.opacity(0.16)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                )
            )
            .overlay(alignment: .top) {
                Rectangle()
                    .fill(tint.opacity(0.7))
                    .frame(height: 1)
            }
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(tint.opacity(0.55), lineWidth: 1)
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
/// the flat jet-black content background — originally the module screens
/// only (Reviews/Labor/Food Cost/Marketing/Intel), now also every modal
/// .sheet() in the app (Ask Cavnar, Notifications, Location Switcher,
/// Templates, Change Password, 2FA Setup, Add Guest, Send Review Request),
/// carrying the same premium feel established for Ask Cavnar's redesign
/// across every other modal instead of leaving them flat. .ignoresSafeArea()
/// so the gradient actually starts behind the status bar/nav bar, not below it.

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
    // Defaults true for every existing plain cavnarEmberBackButton() call
    // site. CavnarTabSwipeNavigation below is the one caller that passes
    // false — while a module is showing its Analytics sub-tab, the edge-
    // swipe-to-pop gesture is disabled entirely so it can't race a plain
    // swipe-right's OWN handling of "go back to the primary sub-tab first"
    // (see that modifier's doc comment for the full reasoning).
    var isEnabled: Bool = true

    func makeUIViewController(context: Context) -> UIViewController {
        UIViewController()
    }

    // The enable/disable toggle lives here, not in makeUIViewController —
    // that only ever runs once, so it could set the gesture's initial
    // state but never react to isEnabled changing afterward. updateUIViewController
    // runs on every SwiftUI re-render, including the one right after
    // creation, so it covers both the initial state and every later toggle.
    func updateUIViewController(_ uiViewController: UIViewController, context: Context) {
        DispatchQueue.main.async {
            guard let nav = uiViewController.navigationController else { return }
            nav.interactivePopGestureRecognizer?.isEnabled = isEnabled
            nav.interactivePopGestureRecognizer?.delegate = nil
        }
    }
}

private struct CavnarEmberBackButton: ViewModifier {
    @Environment(\.dismiss) private var dismiss

    func body(content: Content) -> some View {
        content
            .navigationBarBackButtonHidden(true)
            .background(InteractivePopGestureEnabler().frame(width: 0, height: 0))
            .toolbar {
                cavnarToolbarItem(placement: .navigation) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                            .cavnarToolbarIconGlass()
                    }
                    .buttonStyle(.plain)
                    .tint(nil)
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

/// Replaces cavnarEmberBackButton() for a module screen that has a
/// Tracker/Inbox/Overview + Analytics CavnarSegmentedControl — ties the
/// sub-tab switch into the same "back" vocabulary as real navigation
/// instead of leaving it as a second, disconnected way to move around the
/// screen. Swipe left anywhere jumps to Analytics (the counterpart to the
/// system's own swipe-right-to-go-back). Swipe right, OR tap the back
/// chevron, while already on Analytics returns to the primary tab first;
/// only once actually on the primary tab does either one leave the module.
///
/// Without this, switching to Analytics was invisible to the system: it's
/// just local @State, not a real navigation push, so the interactive pop
/// gesture and the back button both had nothing to undo but the module
/// itself, popping all the way out regardless of which sub-tab was
/// showing. The interactive pop gesture is explicitly disabled while on
/// Analytics (not left running alongside the new swipe-right handling) —
/// UIKit's edge-pan recognizer starts tracking a drag interactively from
/// first touch, while a SwiftUI DragGesture only evaluates on release, so
/// left running together the system gesture would already be mid-pop
/// transition before this view's own onEnded ever got a say.
private struct CavnarTabSwipeNavigation<Tab: Equatable>: ViewModifier {
    @Environment(\.dismiss) private var dismiss
    @Binding var selection: Tab
    let primaryTab: Tab
    let secondaryTab: Tab

    private var isOnSecondary: Bool { selection == secondaryTab }

    func body(content: Content) -> some View {
        content
            .navigationBarBackButtonHidden(true)
            .background(InteractivePopGestureEnabler(isEnabled: !isOnSecondary).frame(width: 0, height: 0))
            .toolbar {
                cavnarToolbarItem(placement: .navigation) {
                    Button {
                        Haptic.light()
                        if isOnSecondary {
                            withAnimation(.easeInOut(duration: 0.2)) { selection = primaryTab }
                        } else {
                            dismiss()
                        }
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                            .cavnarToolbarIconGlass()
                    }
                    .buttonStyle(.plain)
                    .tint(nil)
                }
            }
            // .simultaneousGesture (not .gesture) so this never steals a
            // touch from something more specific already handling it — a
            // ScrollView's own pan, a card's own hold gesture, a button's
            // tap — this only acts in onEnded, once the FULL gesture is
            // already known to have been a clearly horizontal drag past a
            // real threshold, not a vertical scroll that drifted sideways.
            .simultaneousGesture(
                DragGesture(minimumDistance: 40)
                    .onEnded { value in
                        guard abs(value.translation.width) > abs(value.translation.height) * 1.5 else { return }
                        if value.translation.width < -60, selection != secondaryTab {
                            Haptic.light()
                            withAnimation(.easeInOut(duration: 0.2)) { selection = secondaryTab }
                        } else if value.translation.width > 60, isOnSecondary {
                            Haptic.light()
                            withAnimation(.easeInOut(duration: 0.2)) { selection = primaryTab }
                        }
                    }
            )
    }
}

extension View {
    func cavnarTabSwipeNavigation<Tab: Equatable>(_ selection: Binding<Tab>, primaryTab: Tab, secondaryTab: Tab) -> some View {
        modifier(CavnarTabSwipeNavigation(selection: selection, primaryTab: primaryTab, secondaryTab: secondaryTab))
    }
}
