import SwiftUI
import UIKit

// The Cavnar motion language — every animation here follows the same four
// rules the preview page was built on (brand/motion): one point of color per
// surface and it's always the ember; illustrations are hairline Ink3, never
// filled shapes; ease-in-out only, nothing bounces or springs; and each
// animation only ever plays where the app is genuinely working or a state
// genuinely changed. Looping "working" states are driven by TimelineView
// (real wall-clock time) rather than a toggled @State + repeatForever, for
// the reason documented at length on LaborView's ShimmerText: an ambient
// parent transaction silently overrides a repeat-forever animation, and a
// wall-clock phase can't be interrupted by anything.

// MARK: - Seal geometry

/// The seal ring as a real Path — the SealRing asset is a template image,
/// which can be tinted but never trimmed, so anything that needs to DRAW the
/// ring (draw-in, stroke tricks) builds from this instead. Same 120x120
/// source geometry as brand/assets/seal-color.svg: a rounded square with
/// its right edge open between y=45 and y=75, where the ember sits.
struct CavnarSealRingShape: Shape {
    func path(in rect: CGRect) -> Path {
        let s = min(rect.width, rect.height) / 120
        let ox = rect.minX + (rect.width - 120 * s) / 2
        let oy = rect.minY + (rect.height - 120 * s) / 2
        func pt(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: ox + x * s, y: oy + y * s) }
        var p = Path()
        // Starts at the top lip of the gap and travels the whole ring
        // counter-clockwise on screen, ending at the gap's bottom lip —
        // so a trim(from: 0, to: x) reads as the ring drawing itself
        // around from the ember and back to it.
        p.move(to: pt(99.5, 45))
        p.addArc(tangent1End: pt(99.5, 20.5), tangent2End: pt(20.5, 20.5), radius: 24 * s)
        p.addArc(tangent1End: pt(20.5, 20.5), tangent2End: pt(20.5, 99.5), radius: 24 * s)
        p.addArc(tangent1End: pt(20.5, 99.5), tangent2End: pt(99.5, 99.5), radius: 24 * s)
        p.addArc(tangent1End: pt(99.5, 99.5), tangent2End: pt(99.5, 20.5), radius: 24 * s)
        p.addLine(to: pt(99.5, 75))
        return p
    }
}

/// The "hot core" highlight color the ember reaches at full intensity —
/// #F2B183, the same top stop the brand SVGs' ember-dot gradient uses.
let cavnarEmberHot = Color(red: 0.95, green: 0.69, blue: 0.51)

// MARK: - 02 · Seal Draw-in

/// One-shot entrance: the ring draws itself from the gap around and back,
/// then the ember drops in last to close the loop. For a screen's first
/// appearance (login, Face ID gate) — never looped in the real app.
struct CavnarSealDrawIn: View {
    var size: CGFloat = 96
    var ringColor: Color = .cavnarInk
    var delay: Double = 0
    var onFinished: (() -> Void)? = nil

    @State private var ringProgress: CGFloat = 0
    @State private var emberOn = false

    private var emberDiameter: CGFloat { size * (20.0 / 120.0) }
    private var glowDiameter: CGFloat { size * (54.0 / 120.0) }
    private var emberCenter: CGPoint { CGPoint(x: size * (99.5 / 120.0), y: size * (60.0 / 120.0)) }

    var body: some View {
        ZStack {
            CavnarSealRingShape()
                .trim(from: 0, to: ringProgress)
                .stroke(ringColor, style: StrokeStyle(lineWidth: size * (19.0 / 120.0), lineCap: .butt))
                .frame(width: size, height: size)

            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber.opacity(0.5), Color.cavnarEmber.opacity(0)],
                        center: .center, startRadius: 0, endRadius: glowDiameter / 2
                    )
                )
                .frame(width: glowDiameter, height: glowDiameter)
                .opacity(emberOn ? 1 : 0)
                .position(emberCenter)

            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber2, Color.cavnarEmber],
                        center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: emberDiameter * 0.6
                    )
                )
                .frame(width: emberDiameter, height: emberDiameter)
                .scaleEffect(emberOn ? 1 : 0.01)
                .opacity(emberOn ? 1 : 0)
                .position(emberCenter)
        }
        .frame(width: size, height: size)
        .task {
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            withAnimation(.easeInOut(duration: 1.1)) { ringProgress = 1 }
            try? await Task.sleep(for: .seconds(1.0))
            withAnimation(.easeOut(duration: 0.4)) { emberOn = true }
            try? await Task.sleep(for: .seconds(0.4))
            onFinished?()
        }
    }
}

// MARK: - 03 · Wordmark Stamp-in

/// One letter of the CAVNAR wordmark as a Shape. The wordmark is now the
/// app's own headline face — Clash Display Semibold — set in caps and
/// optically kerned, so it's literally the same type every screen title
/// and headline uses rather than a separate custom alphabet. These are
/// that font's real glyph outlines (extracted with fontTools, quadratic
/// curves and all, the font's own contour winding preserved for nonzero
/// fill), placed in a box whose height is the cap height (100 units) —
/// the same geometry brand/assets/wordmark-*.svg and the BrandLockup
/// asset are built from, so this and those are pixel-identical at rest.
struct CavnarWordmarkLetterShape: Shape {
    let index: Int

    static let boxWidth: CGFloat = 654.27
    static let boxHeight: CGFloat = 100
    /// Horizontal center of each letter within the box — the anchor the
    /// stamp-in scales around, so a letter settles in place instead of
    /// sliding sideways toward the box's own center.
    static let centers: [CGFloat] = [52.24, 162.65, 250.12, 367.29, 486.70, 606.13]
    /// Each letter's horizontal extent — the typewriter cursor parks just
    /// past the trailing edge of whatever's been typed so far.
    static let bounds: [(leading: CGFloat, trailing: CGFloat)] = [
        (0, 104.48), (102.79, 222.5), (192.65, 307.58), (319.08, 415.5), (426.85, 546.55), (558, 654.27),
    ]
    /// The ember, cradled in the V's opening — same spot the brand SVGs
    /// draw it (brand/assets/wordmark.json "ember").
    static let emberCenter = CGPoint(x: 250.1, y: 28)
    static let emberRadius: CGFloat = 11

    func path(in rect: CGRect) -> Path {
        let s = rect.width / Self.boxWidth
        func pt(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: rect.minX + x * s, y: rect.minY + y * s) }
        var p = Path()
        // Generated from ClashDisplay-Semibold.ttf (see brand/assets and
        // the wordmark.json the SVGs share) — do not hand-edit; regenerate.
        switch index {
        case 0:
            p.move(to: pt(53.43, 101.49))
            p.addQuadCurve(to: pt(14.40, 87.46), control: pt(28.81, 101.49))
            p.addQuadCurve(to: pt(0.00, 50.00), control: pt(0.00, 73.43))
            p.addQuadCurve(to: pt(14.40, 12.54), control: pt(0.00, 26.57))
            p.addQuadCurve(to: pt(53.43, -1.49), control: pt(28.81, -1.49))
            p.addQuadCurve(to: pt(90.67, 9.48), control: pt(76.87, -1.49))
            p.addQuadCurve(to: pt(104.48, 39.40), control: pt(104.48, 20.45))
            p.addLine(to: pt(104.48, 41.64))
            p.addLine(to: pt(79.55, 41.64))
            p.addLine(to: pt(79.55, 39.40))
            p.addQuadCurve(to: pt(73.43, 24.70), control: pt(79.55, 29.40))
            p.addQuadCurve(to: pt(53.88, 20.00), control: pt(67.31, 20.00))
            p.addQuadCurve(to: pt(30.52, 26.72), control: pt(37.31, 20.00))
            p.addQuadCurve(to: pt(23.73, 50.00), control: pt(23.73, 33.43))
            p.addQuadCurve(to: pt(30.52, 73.28), control: pt(23.73, 66.57))
            p.addQuadCurve(to: pt(53.88, 80.00), control: pt(37.31, 80.00))
            p.addQuadCurve(to: pt(73.43, 75.30), control: pt(67.31, 80.00))
            p.addQuadCurve(to: pt(79.55, 60.60), control: pt(79.55, 70.60))
            p.addLine(to: pt(79.55, 58.36))
            p.addLine(to: pt(104.48, 58.36))
            p.addLine(to: pt(104.48, 60.60))
            p.addQuadCurve(to: pt(90.67, 90.52), control: pt(104.48, 79.55))
            p.addQuadCurve(to: pt(53.43, 101.49), control: pt(76.87, 101.49))
            p.closeSubpath()
        case 1:
            p.move(to: pt(127.87, 100.00))
            p.addLine(to: pt(102.79, 100.00))
            p.addLine(to: pt(146.52, 0.00))
            p.addLine(to: pt(178.47, 0.00))
            p.addLine(to: pt(222.50, 100.00))
            p.addLine(to: pt(196.82, 100.00))
            p.addLine(to: pt(187.42, 77.91))
            p.addLine(to: pt(137.42, 77.91))
            p.closeSubpath()
            p.move(to: pt(155.48, 35.97))
            p.addLine(to: pt(146.08, 57.76))
            p.addLine(to: pt(178.76, 57.76))
            p.addLine(to: pt(169.36, 35.97))
            p.addLine(to: pt(163.24, 20.45))
            p.addLine(to: pt(161.60, 20.45))
            p.closeSubpath()
        case 2:
            p.move(to: pt(266.08, 100.00))
            p.addLine(to: pt(234.14, 100.00))
            p.addLine(to: pt(192.65, 0.00))
            p.addLine(to: pt(219.67, 0.00))
            p.addLine(to: pt(249.67, 77.31))
            p.addLine(to: pt(251.16, 77.31))
            p.addLine(to: pt(280.71, 0.00))
            p.addLine(to: pt(307.58, 0.00))
            p.closeSubpath()
        case 3:
            p.move(to: pt(341.47, 100.00))
            p.addLine(to: pt(319.08, 100.00))
            p.addLine(to: pt(319.08, 0.00))
            p.addLine(to: pt(342.96, 0.00))
            p.addLine(to: pt(378.78, 47.31))
            p.addLine(to: pt(392.51, 68.36))
            p.addLine(to: pt(394.15, 68.36))
            p.addLine(to: pt(393.11, 48.21))
            p.addLine(to: pt(393.11, 0.00))
            p.addLine(to: pt(415.50, 0.00))
            p.addLine(to: pt(415.50, 100.00))
            p.addLine(to: pt(391.61, 100.00))
            p.addLine(to: pt(354.75, 52.24))
            p.addLine(to: pt(342.06, 33.43))
            p.addLine(to: pt(340.57, 33.43))
            p.addLine(to: pt(341.47, 51.79))
            p.closeSubpath()
        case 4:
            p.move(to: pt(451.92, 100.00))
            p.addLine(to: pt(426.85, 100.00))
            p.addLine(to: pt(470.58, 0.00))
            p.addLine(to: pt(502.52, 0.00))
            p.addLine(to: pt(546.55, 100.00))
            p.addLine(to: pt(520.88, 100.00))
            p.addLine(to: pt(511.47, 77.91))
            p.addLine(to: pt(461.47, 77.91))
            p.closeSubpath()
            p.move(to: pt(479.53, 35.97))
            p.addLine(to: pt(470.13, 57.76))
            p.addLine(to: pt(502.82, 57.76))
            p.addLine(to: pt(493.41, 35.97))
            p.addLine(to: pt(487.29, 20.45))
            p.addLine(to: pt(485.65, 20.45))
            p.closeSubpath()
        default:
            p.move(to: pt(580.39, 100.00))
            p.addLine(to: pt(558.00, 100.00))
            p.addLine(to: pt(558.00, 0.00))
            p.addLine(to: pt(613.22, 0.00))
            p.addQuadCurve(to: pt(641.80, 7.84), control: pt(631.73, 0.00))
            p.addQuadCurve(to: pt(651.88, 30.00), control: pt(651.88, 15.67))
            p.addQuadCurve(to: pt(623.67, 57.76), control: pt(651.88, 55.07))
            p.addLine(to: pt(623.67, 58.96))
            p.addQuadCurve(to: pt(633.30, 63.88), control: pt(629.94, 60.60))
            p.addQuadCurve(to: pt(639.79, 73.13), control: pt(636.65, 67.16))
            p.addLine(to: pt(654.27, 100.00))
            p.addLine(to: pt(628.30, 100.00))
            p.addLine(to: pt(614.56, 74.03))
            p.addQuadCurve(to: pt(607.55, 65.90), control: pt(611.43, 68.06))
            p.addQuadCurve(to: pt(595.16, 63.73), control: pt(603.67, 63.73))
            p.addLine(to: pt(580.39, 63.73))
            p.closeSubpath()
            p.move(to: pt(580.39, 20.15))
            p.addLine(to: pt(580.39, 46.87))
            p.addLine(to: pt(613.07, 46.87))
            p.addQuadCurve(to: pt(624.86, 43.96), control: pt(621.28, 46.87))
            p.addQuadCurve(to: pt(628.45, 33.43), control: pt(628.45, 41.04))
            p.addQuadCurve(to: pt(624.79, 23.13), control: pt(628.45, 26.12))
            p.addQuadCurve(to: pt(613.07, 20.15), control: pt(621.13, 20.15))
            p.closeSubpath()
        }
        return p
    }
}

/// The ember that the V cradles — the one point of color inside the
/// wordmark itself, shared by the stamp-in and typewriter entrances.
struct CavnarWordmarkEmber: View {
    var scale: CGFloat

    var body: some View {
        ZStack {
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber.opacity(0.28), Color.cavnarEmber.opacity(0)],
                        center: .center, startRadius: 0, endRadius: CavnarWordmarkLetterShape.emberRadius * 2.2 * scale
                    )
                )
                .frame(width: CavnarWordmarkLetterShape.emberRadius * 4.4 * scale, height: CavnarWordmarkLetterShape.emberRadius * 4.4 * scale)
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber2, Color.cavnarEmber],
                        center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: CavnarWordmarkLetterShape.emberRadius * 1.2 * scale
                    )
                )
                .frame(width: CavnarWordmarkLetterShape.emberRadius * 2 * scale, height: CavnarWordmarkLetterShape.emberRadius * 2 * scale)
        }
    }
}

/// Six letters stamp in like a branding iron, one after another, the
/// ember drops into the V, then the AI tag fades up beside them. Plays
/// once on appear. `width` is the wordmark's own width (the AI tag sits
/// outside it, to the right).
struct CavnarWordmarkStampIn: View {
    var width: CGFloat
    var color: Color = .cavnarInk
    var delay: Double = 0
    var showsAITag: Bool = true
    // When true the AI tag hangs off the wordmark's trailing edge as an
    // overlay that takes no layout width — so centering this view centers
    // the six letters themselves, with the tag sitting off to the right
    // (the lock screen). Off by default: inside CavnarLockupIntro the tag
    // is part of the composed lockup's width, matching the BrandLockup asset.
    var aiTagOverhangs: Bool = false

    @State private var shown: [Bool] = Array(repeating: false, count: 6)
    @State private var emberDropped = false
    @State private var tagShown = false

    private var scale: CGFloat { width / CavnarWordmarkLetterShape.boxWidth }
    private var height: CGFloat { CavnarWordmarkLetterShape.boxHeight * scale }

    var body: some View {
        HStack(alignment: .top, spacing: 54 * scale) {
            ZStack(alignment: .topLeading) {
                ForEach(0..<6, id: \.self) { i in
                    CavnarWordmarkLetterShape(index: i)
                        .fill(color)
                        .frame(width: width, height: height)
                        .opacity(shown[i] ? 1 : 0)
                        .scaleEffect(
                            shown[i] ? 1 : 1.06,
                            anchor: UnitPoint(x: CavnarWordmarkLetterShape.centers[i] / CavnarWordmarkLetterShape.boxWidth, y: 0.5)
                        )
                        .offset(y: shown[i] ? 0 : 8 * scale)
                }
                CavnarWordmarkEmber(scale: scale)
                    .opacity(emberDropped ? 1 : 0)
                    .offset(y: emberDropped ? 0 : -22 * scale)
                    .position(x: CavnarWordmarkLetterShape.emberCenter.x * scale, y: CavnarWordmarkLetterShape.emberCenter.y * scale)
            }
            .frame(width: width, height: height)
            .overlay(alignment: .topLeading) {
                if showsAITag && aiTagOverhangs {
                    // Pinned at the frame's leading edge, then pushed past
                    // the whole wordmark plus one gap — outside the frame,
                    // taking no layout width. (An alignmentGuide on the
                    // trailing edge was tried first and landed on the R.)
                    aiTag.offset(x: width + 54 * scale)
                }
            }

            if showsAITag && !aiTagOverhangs {
                aiTag
            }
        }
        .task {
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            for i in 0..<6 {
                withAnimation(.easeOut(duration: 0.32)) { shown[i] = true }
                try? await Task.sleep(for: .seconds(0.09))
            }
            try? await Task.sleep(for: .seconds(0.25))
            withAnimation(.easeOut(duration: 0.35)) { emberDropped = true }
            try? await Task.sleep(for: .seconds(0.2))
            withAnimation(.easeOut(duration: 0.4)) { tagShown = true }
        }
    }

    // Same size/tracking ratio as the BrandLockup asset's own tag
    // (font-size 30, letter-spacing 6, in the same 100-unit box).
    private var aiTag: some View {
        Text("AI")
            .font(.cavnarNumber(30 * scale, weight: 700))
            .tracking(6 * scale)
            .foregroundStyle(Color.cavnarEmber)
            .padding(.top, 6 * scale)
            .opacity(tagShown ? 1 : 0)
    }
}

/// The wordmark typed out by an ember cursor — the cold-launch entrance
/// (a fresh process, not a return from the background). The cursor
/// appears where the C will land, blinks once, types the six letters at
/// a quick clip (each letter snaps in whole — no fade — the way a caret
/// commits a character), the ember drops into the V, the cursor blinks
/// twice at the end of the word and fades away, then the AI tag fades up.
struct CavnarWordmarkTypewriter: View {
    var width: CGFloat
    var color: Color = .cavnarInk
    var delay: Double = 0
    var showsAITag: Bool = true
    var aiTagOverhangs: Bool = false
    var onFinished: (() -> Void)? = nil

    @State private var typed = 0
    @State private var cursorShown = false
    @State private var cursorLit = true
    @State private var emberDropped = false
    @State private var tagShown = false

    private var scale: CGFloat { width / CavnarWordmarkLetterShape.boxWidth }
    private var height: CGFloat { CavnarWordmarkLetterShape.boxHeight * scale }
    private var cursorX: CGFloat {
        typed == 0 ? 0 : (CavnarWordmarkLetterShape.bounds[typed - 1].trailing + 8) * scale
    }

    var body: some View {
        HStack(alignment: .top, spacing: 54 * scale) {
            ZStack(alignment: .topLeading) {
                ForEach(0..<6, id: \.self) { i in
                    CavnarWordmarkLetterShape(index: i)
                        .fill(color)
                        .frame(width: width, height: height)
                        .opacity(i < typed ? 1 : 0)
                }
                CavnarWordmarkEmber(scale: scale)
                    .opacity(emberDropped ? 1 : 0)
                    .offset(y: emberDropped ? 0 : -22 * scale)
                    .position(x: CavnarWordmarkLetterShape.emberCenter.x * scale, y: CavnarWordmarkLetterShape.emberCenter.y * scale)
                // The cursor: a cap-height ember bar, one letter-stroke wide.
                RoundedRectangle(cornerRadius: 2 * scale)
                    .fill(Color.cavnarEmber)
                    .frame(width: 9 * scale, height: height)
                    .shadow(color: Color.cavnarEmber.opacity(0.6), radius: 6 * scale)
                    .offset(x: cursorX)
                    .opacity(cursorShown && cursorLit ? 1 : 0)
            }
            .frame(width: width, height: height)
            .overlay(alignment: .topLeading) {
                if showsAITag && aiTagOverhangs {
                    aiTag.offset(x: width + 54 * scale)
                }
            }

            if showsAITag && !aiTagOverhangs {
                aiTag
            }
        }
        .task {
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            // Cursor arrives where the C will be, blinks once.
            withAnimation(.easeOut(duration: 0.2)) { cursorShown = true }
            try? await Task.sleep(for: .seconds(0.45))
            cursorLit = false
            try? await Task.sleep(for: .seconds(0.18))
            cursorLit = true
            try? await Task.sleep(for: .seconds(0.3))
            // Type.
            for i in 1...6 {
                var t = Transaction(animation: nil)
                t.disablesAnimations = true
                withTransaction(t) { typed = i }
                try? await Task.sleep(for: .seconds(0.1))
            }
            try? await Task.sleep(for: .seconds(0.15))
            withAnimation(.easeOut(duration: 0.35)) { emberDropped = true }
            // Two blinks at the end of the word, then gone.
            for _ in 0..<2 {
                try? await Task.sleep(for: .seconds(0.32))
                cursorLit = false
                try? await Task.sleep(for: .seconds(0.3))
                cursorLit = true
            }
            try? await Task.sleep(for: .seconds(0.35))
            withAnimation(.easeOut(duration: 0.55)) { cursorShown = false }
            try? await Task.sleep(for: .seconds(0.2))
            withAnimation(.easeOut(duration: 0.4)) { tagShown = true }
            try? await Task.sleep(for: .seconds(0.4))
            onFinished?()
        }
    }

    private var aiTag: some View {
        Text("AI")
            .font(.cavnarNumber(30 * scale, weight: 700))
            .tracking(6 * scale)
            .foregroundStyle(Color.cavnarEmber)
            .padding(.top, 6 * scale)
            .opacity(tagShown ? 1 : 0)
    }
}

/// The full lockup (seal + wordmark + AI tag) as one choreographed
/// entrance — seal draws itself while the letters stamp in (or, on a cold
/// launch, get typed out by the ember cursor) beside it. Same 920x148
/// proportions as the BrandLockup asset so it drops in at the same `width`
/// wherever that static image was used.
struct CavnarLockupIntro: View {
    var width: CGFloat
    var typewriter: Bool = false

    private var s: CGFloat { width / 920 }

    var body: some View {
        HStack(alignment: .top, spacing: 30 * s) {
            CavnarSealDrawIn(size: 120 * s)
                .padding(.top, 14 * s)
            Group {
                if typewriter {
                    CavnarWordmarkTypewriter(width: CavnarWordmarkLetterShape.boxWidth * s, delay: 0.45)
                } else {
                    CavnarWordmarkStampIn(width: CavnarWordmarkLetterShape.boxWidth * s, delay: 0.45)
                }
            }
            .padding(.top, 4 * s)
        }
        .frame(width: width, height: 148 * s, alignment: .topLeading)
    }
}

// MARK: - 04 · Composing

/// The wait BEFORE an AI-written paragraph arrives: an ember caret writes
/// each line into place, one after another, then holds and starts over.
/// Replaces the neutral skeleton bars for any Claude call that returns
/// prose (review reply draft, Ask Cavnar, marketing copy). TypewriterText
/// still handles the reveal once the words are actually here.
struct CavnarComposingLines: View {
    var widths: [CGFloat] = [1.0, 0.82, 0.93, 0.6]
    var lineHeight: CGFloat = 10
    var spacing: CGFloat = 12
    var tint: Color = .cavnarEmber

    @State private var start = Date()

    private static let grow: Double = 0.85
    private static let hold: Double = 1.1
    private static let fade: Double = 0.3

    private var totalHeight: CGFloat {
        CGFloat(widths.count) * lineHeight + CGFloat(max(widths.count - 1, 0)) * spacing
    }

    private func progress(_ local: Double, _ i: Int) -> CGFloat {
        let x = min(max((local - Double(i) * Self.grow) / Self.grow, 0), 1)
        return CGFloat(1 - pow(1 - x, 3))
    }

    var body: some View {
        TimelineView(.animation) { timeline in
            let t = timeline.date.timeIntervalSince(start)
            let n = Double(widths.count)
            let cycle = n * Self.grow + Self.hold + Self.fade
            let local = t.truncatingRemainder(dividingBy: cycle)
            let fadeOut = min(max((local - (n * Self.grow + Self.hold)) / Self.fade, 0), 1)
            let active = min(Int(local / Self.grow), widths.count - 1)
            let holding = local >= n * Self.grow
            let blinkOn = !holding || (t.truncatingRemainder(dividingBy: 0.7) < 0.38)

            GeometryReader { geo in
                ZStack(alignment: .topLeading) {
                    VStack(alignment: .leading, spacing: spacing) {
                        ForEach(widths.indices, id: \.self) { i in
                            RoundedRectangle(cornerRadius: lineHeight / 2)
                                .fill(tint.opacity(0.28))
                                .frame(width: max(2, geo.size.width * widths[i] * progress(local, i)), height: lineHeight)
                        }
                    }
                    RoundedRectangle(cornerRadius: 1)
                        .fill(Color.cavnarEmber2)
                        .frame(width: 2.5, height: lineHeight + 5)
                        .opacity(blinkOn ? 1 : 0)
                        .offset(
                            x: geo.size.width * widths[active] * progress(local, active) + 3,
                            y: CGFloat(active) * (lineHeight + spacing) - 2.5
                        )
                }
                .opacity(1 - fadeOut)
            }
        }
        .frame(height: totalHeight)
    }
}

// MARK: - 05 · Reading the Room

/// The 20–40s competitor-intel / AI-visibility wait as a picture of what it
/// is doing: a radar centered on the restaurant (the square) with ripples
/// going out and competitors appearing as ember blips. No sweep wedge —
/// just the ripples and the blips.
struct CavnarRadarSweep: View {
    var size: CGFloat = 170
    var caption: String? = nil

    @State private var start = Date()

    // (dx, dy) as fractions of `size` from center, plus each blip's delay
    // into the 6s cycle.
    private static let blips: [(CGFloat, CGFloat, Double)] = [
        (0.26, -0.2, 0.6), (-0.24, 0.16, 1.9), (0.3, 0.22, 3.1), (-0.17, -0.26, 4.2),
    ]

    private func blipState(_ phase: Double) -> (scale: CGFloat, opacity: Double) {
        switch phase {
        case ..<0.0: return (0, 0)
        case ..<0.08: let x = phase / 0.08; return (CGFloat(1.3 * x), x)
        case ..<0.14: let x = (phase - 0.08) / 0.06; return (CGFloat(1.3 - 0.3 * x), 1)
        case ..<0.7: return (1, 1)
        case ..<0.82: let x = (phase - 0.7) / 0.12; return (CGFloat(1 - 0.4 * x), 1 - x)
        default: return (0, 0)
        }
    }

    var body: some View {
        VStack(spacing: 16) {
            TimelineView(.animation) { timeline in
                let t = timeline.date.timeIntervalSince(start)
                ZStack {
                    ForEach([0.12, 0.3, 0.48], id: \.self) { r in
                        Circle()
                            .stroke(Color.cavnarPaper3.opacity(r == 0.12 ? 0.9 : 0.55), lineWidth: 1)
                            .frame(width: size * 2 * r, height: size * 2 * r)
                    }
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.45)).frame(width: size * 0.96, height: 1)
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.45)).frame(width: 1, height: size * 0.96)

                    ForEach(0..<3, id: \.self) { k in
                        let raw = ((t + 3.0 - Double(k)).truncatingRemainder(dividingBy: 3.0)) / 3.0
                        let eased = 1 - pow(1 - raw, 2)
                        Circle()
                            .stroke(Color.cavnarEmber.opacity(0.9 * (1 - raw)), lineWidth: 1.2)
                            .frame(width: size * (0.06 + 0.9 * eased), height: size * (0.06 + 0.9 * eased))
                    }

                    ForEach(Self.blips.indices, id: \.self) { i in
                        let b = Self.blips[i]
                        let phase = ((t + 6.0 - b.2).truncatingRemainder(dividingBy: 6.0)) / 6.0
                        let state = blipState(phase)
                        ZStack {
                            Circle().fill(Color.cavnarEmber.opacity(0.25)).frame(width: 18, height: 18)
                            Circle().fill(Color.cavnarEmber2).frame(width: 9, height: 9)
                        }
                        .scaleEffect(state.scale)
                        .opacity(state.opacity)
                        .offset(x: size * b.0, y: size * b.1)
                    }

                    RoundedRectangle(cornerRadius: 1.5)
                        .fill(Color.cavnarInk)
                        .frame(width: size * 0.07, height: size * 0.07)
                }
                .frame(width: size, height: size)
            }
            .frame(width: size, height: size)

            if let caption {
                Text(caption.uppercased())
                    .font(.cavnarNumber(14, weight: 600))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - 06 · Building the Week

/// The schedule-generation wait: shifts fill a 7-day grid row by row while
/// an ember dash travels the header line. Ember blocks are the ones the AI
/// is "still deciding." Fills whatever width it's given.
struct CavnarWeekBuilder: View {
    var caption: String? = nil

    @State private var start = Date()

    private static let days = ["S", "M", "T", "W", "T", "F", "S"]
    private static let blocks: [(row: Int, col: Int, ember: Bool)] = [
        (0, 0, false), (0, 1, false), (0, 3, false), (0, 5, false), (0, 6, false),
        (1, 1, false), (1, 2, false), (1, 4, false), (1, 5, false),
        (2, 0, false), (2, 2, false), (2, 3, false), (2, 6, false),
        (2, 4, true), (3, 6, true), (3, 1, false), (3, 3, true),
    ]
    private static let stagger: Double = 0.16
    private static let grow: Double = 0.45
    private static let hold: Double = 1.6
    private static let fade: Double = 0.3
    private static let rowHeight: CGFloat = 24
    private static let gridTop: CGFloat = 34

    private var contentHeight: CGFloat { Self.gridTop + 4 * Self.rowHeight + (caption == nil ? 0 : 20) }

    private func blockProgress(_ local: Double, _ i: Int) -> CGFloat {
        let x = min(max((local - Double(i) * Self.stagger) / Self.grow, 0), 1)
        return CGFloat(1 - pow(1 - x, 3))
    }

    var body: some View {
        TimelineView(.animation) { timeline in
            let t = timeline.date.timeIntervalSince(start)
            let blocksDone = Double(Self.blocks.count - 1) * Self.stagger + Self.grow
            let cycle = blocksDone + Self.hold + Self.fade
            let local = t.truncatingRemainder(dividingBy: cycle)
            let fadeOut = min(max((local - (blocksDone + Self.hold)) / Self.fade, 0), 1)
            let dashPhase = (t / 2.2).truncatingRemainder(dividingBy: 1)

            GeometryReader { geo in
                let colWidth = geo.size.width / 7
                let blockWidth = colWidth * 0.7
                let dashLength = geo.size.width * 0.16
                ZStack(alignment: .topLeading) {
                    Capsule().fill(Color.cavnarPaper3.opacity(0.8)).frame(height: 1.5)
                    Capsule()
                        .fill(LinearGradient(colors: [.clear, Color.cavnarEmber2, .clear], startPoint: .leading, endPoint: .trailing))
                        .frame(width: dashLength, height: 2.5)
                        .offset(x: -dashLength + CGFloat(dashPhase) * (geo.size.width + dashLength), y: -0.5)

                    HStack(spacing: 0) {
                        ForEach(Self.days.indices, id: \.self) { i in
                            Text(Self.days[i])
                                .font(.cavnarNumber(14, weight: 600))
                                .foregroundStyle(Color.cavnarInk3)
                                .frame(width: colWidth)
                        }
                    }
                    .offset(y: 10)

                    ForEach(Self.blocks.indices, id: \.self) { i in
                        let b = Self.blocks[i]
                        let p = blockProgress(local, i)
                        RoundedRectangle(cornerRadius: 4)
                            .fill(b.ember ? Color.cavnarEmber : Color.cavnarInk3.opacity(0.35))
                            .frame(width: blockWidth, height: 16)
                            .offset(
                                x: colWidth * CGFloat(b.col) + (colWidth - blockWidth) / 2 - 14 * (1 - p),
                                y: Self.gridTop + CGFloat(b.row) * Self.rowHeight
                            )
                            .opacity(p)
                    }

                    if let caption {
                        Text(caption.uppercased())
                            .font(.cavnarNumber(14, weight: 600))
                            .tracking(1)
                            .foregroundStyle(Color.cavnarInk3)
                            .offset(y: Self.gridTop + 4 * Self.rowHeight + 2)
                    }
                }
                .opacity(1 - fadeOut)
            }
        }
        .frame(height: contentHeight)
        .clipped()
    }
}

// MARK: - 07 · Counting the Pantry

/// The inventory-analysis wait as a ledger filling in category by category,
/// with the over-budget one in ember and a sheen sweep as the audit pass.
struct CavnarLedgerFill: View {
    struct Row {
        let label: String
        let fraction: CGFloat
        let ember: Bool
        init(_ label: String, _ fraction: CGFloat, ember: Bool = false) {
            self.label = label; self.fraction = fraction; self.ember = ember
        }
    }

    var rows: [Row] = [
        Row("PRODUCE", 0.82), Row("PROTEIN", 0.94), Row("DAIRY", 0.55), Row("DRY", 0.69), Row("BAR", 0.38, ember: true),
    ]

    @State private var start = Date()

    private static let stagger: Double = 0.3
    private static let grow: Double = 0.9
    private static let hold: Double = 2.9
    private static let fade: Double = 0.3

    private func rowProgress(_ local: Double, _ i: Int) -> CGFloat {
        let x = min(max((local - Double(i) * Self.stagger) / Self.grow, 0), 1)
        return CGFloat(1 - pow(1 - x, 3))
    }

    var body: some View {
        TimelineView(.animation) { timeline in
            let t = timeline.date.timeIntervalSince(start)
            let barsDone = Double(rows.count - 1) * Self.stagger + Self.grow
            let cycle = barsDone + Self.hold + Self.fade
            let local = t.truncatingRemainder(dividingBy: cycle)
            let fadeOut = min(max((local - (barsDone + Self.hold)) / Self.fade, 0), 1)
            // Sheen starts once the first bars are up and sweeps every 2.6s.
            let sheen = local < 1.6 ? -1.0 : ((local - 1.6).truncatingRemainder(dividingBy: 2.6)) / 1.1

            VStack(spacing: 14) {
                ForEach(rows.indices, id: \.self) { i in
                    HStack(spacing: 14) {
                        Text(rows[i].label)
                            .font(.cavnarNumber(14, weight: 600))
                            .tracking(1)
                            .foregroundStyle(Color.cavnarInk3)
                            .lineLimit(1)
                            .fixedSize()
                            .frame(width: 92, alignment: .leading)
                        GeometryReader { geo in
                            ZStack(alignment: .leading) {
                                Capsule().fill(Color.cavnarPaper3.opacity(0.35))
                                Capsule()
                                    .fill(rows[i].ember ? Color.cavnarEmber : Color.cavnarInk3.opacity(0.45))
                                    .frame(width: geo.size.width * rows[i].fraction * rowProgress(local, i))
                            }
                        }
                        .frame(height: 10)
                    }
                }
            }
            .overlay {
                GeometryReader { geo in
                    if sheen >= 0, sheen <= 1 {
                        LinearGradient(
                            colors: [.clear, Color.cavnarInk.opacity(0.12), .clear],
                            startPoint: .leading, endPoint: .trailing
                        )
                        .frame(width: 34)
                        .offset(x: 64 + 14 - 34 + CGFloat(sheen) * (geo.size.width - 64 - 14 + 34))
                    }
                }
                .clipped()
                .allowsHitTesting(false)
            }
            .opacity(1 - fadeOut)
        }
    }
}

// MARK: - 11 · Posted

private struct CavnarCheckShape: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.minX + rect.width * 0.28, y: rect.minY + rect.height * 0.52))
        p.addLine(to: CGPoint(x: rect.minX + rect.width * 0.44, y: rect.minY + rect.height * 0.68))
        p.addLine(to: CGPoint(x: rect.minX + rect.width * 0.73, y: rect.minY + rect.height * 0.36))
        return p
    }
}

/// The confirmation moment for a real POST that succeeded: the ember leaves
/// the draft, travels the wire, and lands as a checkmark that draws itself.
/// Plays once on appear; `onFinished` fires ~1.6s in, once the check has
/// fully drawn, for a caller that wants to dismiss afterward.
struct CavnarPostedCheck: View {
    var label: String
    var onFinished: (() -> Void)? = nil

    @State private var travel: CGFloat = 0
    @State private var landed = false
    @State private var checkTrim: CGFloat = 0
    @State private var labelShown = false

    private let trackWidth: CGFloat = 110

    var body: some View {
        VStack(spacing: 14) {
            HStack(spacing: 0) {
                // The draft — three hairlines on a plate.
                VStack(alignment: .leading, spacing: 5) {
                    Capsule().fill(Color.cavnarInk3.opacity(0.5)).frame(width: 30, height: 2.5)
                    Capsule().fill(Color.cavnarInk3.opacity(0.5)).frame(width: 20, height: 2.5)
                    Capsule().fill(Color.cavnarInk3.opacity(0.5)).frame(width: 26, height: 2.5)
                }
                .padding(.horizontal, 12)
                .frame(width: 56, height: 40, alignment: .leading)
                .background(Color.cavnarPaper2)
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 10))

                ZStack(alignment: .leading) {
                    Capsule().fill(Color.cavnarPaper3).frame(width: trackWidth, height: 2)
                    Capsule().fill(Color.cavnarEmber).frame(width: trackWidth * travel, height: 2)
                    ZStack {
                        Circle().fill(Color.cavnarEmber.opacity(0.3)).frame(width: 22, height: 22)
                        Circle().fill(Color.cavnarEmber2).frame(width: 10, height: 10)
                    }
                    .offset(x: -11 + trackWidth * travel)
                    .opacity(landed ? 0 : 1)
                }
                .frame(width: trackWidth, height: 22)

                ZStack {
                    Circle()
                        .fill(Color.cavnarPaper2)
                        .overlay(Circle().strokeBorder(landed ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1.5))
                    CavnarCheckShape()
                        .trim(from: 0, to: checkTrim)
                        .stroke(Color.cavnarInk, style: StrokeStyle(lineWidth: 2.4, lineCap: .round, lineJoin: .round))
                }
                .frame(width: 40, height: 40)
                .scaleEffect(landed ? 1 : 0.96)
            }

            Text(label.uppercased())
                .font(.cavnarNumber(14, weight: 600))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarInk3)
                .opacity(labelShown ? 1 : 0)
        }
        .task {
            // Deliberately unhurried — the ember crossing the wire is the
            // whole point of the moment, so it gets a real beat.
            withAnimation(.easeInOut(duration: 1.15)) { travel = 1 }
            try? await Task.sleep(for: .seconds(1.1))
            withAnimation(.easeOut(duration: 0.3)) { landed = true }
            withAnimation(.easeInOut(duration: 0.6).delay(0.08)) { checkTrim = 1 }
            withAnimation(.easeOut(duration: 0.4).delay(0.3)) { labelShown = true }
            try? await Task.sleep(for: .seconds(1.3))
            onFinished?()
        }
    }
}

// MARK: - 12 · Handshake

enum CavnarHandshakeState {
    case connecting, connected, failed
}

private struct CavnarHorizontalLine: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.minX, y: rect.midY))
        p.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
        return p
    }
}

/// A credential check / OAuth round trip in flight: dashes march between
/// the seal and the provider tile while the token is verified; the line
/// goes solid ember and both tiles light on success. A failed handshake just
/// stops marching.
struct CavnarHandshake: View {
    var providerSymbol: String
    var providerTint: Color
    var state: CavnarHandshakeState
    var caption: String? = nil

    @State private var start = Date()

    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 0) {
                tile { CavnarSealMark(size: 26) }

                ZStack {
                    TimelineView(.animation(paused: state != .connecting)) { timeline in
                        let phase = timeline.date.timeIntervalSince(start).truncatingRemainder(dividingBy: 0.6) / 0.6
                        CavnarHorizontalLine()
                            .stroke(
                                Color.cavnarInk3.opacity(0.6),
                                style: StrokeStyle(lineWidth: 2, lineCap: .round, dash: [6, 6], dashPhase: -12 * CGFloat(phase))
                            )
                    }
                    .opacity(state == .connecting ? 1 : 0)

                    CavnarHorizontalLine()
                        .stroke(state == .failed ? Color.cavnarRed.opacity(0.5) : Color.cavnarEmber, lineWidth: 2)
                        .opacity(state == .connecting ? 0 : 1)

                    if state != .connecting {
                        ZStack {
                            Circle()
                                .fill(state == .connected ? Color.cavnarEmber : Color.cavnarRed)
                                .frame(width: 22, height: 22)
                            Image(systemName: state == .connected ? "checkmark" : "xmark")
                                .font(.system(size: 10, weight: .bold))
                                .foregroundStyle(.white)
                        }
                        .transition(.scale(scale: 0.4).combined(with: .opacity))
                    }
                }
                .frame(height: 22)
                .padding(.horizontal, 4)

                tile {
                    Image(systemName: providerSymbol)
                        .font(.system(size: 17))
                        .foregroundStyle(providerTint)
                }
            }
            .animation(.easeOut(duration: 0.35), value: state)

            if let caption {
                Text(caption.uppercased())
                    .font(.cavnarNumber(14, weight: 600))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func tile<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        content()
            .frame(width: 52, height: 52)
            .background(Color.cavnarPaper2)
            .overlay(
                RoundedRectangle(cornerRadius: 14)
                    .strokeBorder(state == .connected ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1.5)
            )
            .clipShape(RoundedRectangle(cornerRadius: 14))
    }
}

// MARK: - 13 · Alert Fired

/// One or two ember rings expanding outward from a point, once, on appear —
/// the "something just landed here" cue behind a badge or an alert icon.
struct CavnarRippleBurst: View {
    var color: Color = .cavnarEmber
    var fromDiameter: CGFloat = 14
    var toDiameter: CGFloat = 52
    var rings: Int = 2
    var stagger: Double = 0.45
    var duration: Double = 1.1
    var delay: Double = 0

    @State private var fired = false

    var body: some View {
        ZStack {
            ForEach(0..<rings, id: \.self) { i in
                Circle()
                    .stroke(color, lineWidth: 1.4)
                    .frame(width: fired ? toDiameter : fromDiameter, height: fired ? toDiameter : fromDiameter)
                    .opacity(fired ? 0 : 0.85)
                    .animation(.easeOut(duration: duration).delay(delay + Double(i) * stagger), value: fired)
            }
        }
        .allowsHitTesting(false)
        .onAppear {
            // Deferred one run-loop turn so the collapsed frame commits
            // first and there's an observable change to animate from.
            DispatchQueue.main.async { fired = true }
        }
    }
}

/// The unread dot on the bell — pops in with a single ripple the moment it
/// appears, instead of just being there.
struct CavnarAlertBadge: View {
    var diameter: CGFloat = 8

    @State private var shown = false

    var body: some View {
        ZStack {
            CavnarRippleBurst(fromDiameter: diameter, toDiameter: diameter * 4, rings: 1, duration: 0.9, delay: 0.1)
            Circle()
                .fill(Color.cavnarEmber)
                .frame(width: diameter, height: diameter)
                .scaleEffect(shown ? 1 : 0.01)
        }
        .frame(width: diameter, height: diameter)
        .onAppear {
            DispatchQueue.main.async {
                withAnimation(.easeOut(duration: 0.35)) { shown = true }
            }
        }
    }
}

// MARK: - 14 · Cold Hearth

/// The one empty state for the whole app: a gray seal with a cold ember —
/// "nothing here yet" — that the CTA is what lights. Replaces the scattered
/// per-screen SF Symbol ContentUnavailableViews. With no CTA it just keeps
/// a slow, faint breath so it never reads as dead.
struct CavnarEmptyHearth: View {
    var title: String
    var message: String? = nil
    var ctaLabel: String? = nil
    var action: (() -> Void)? = nil

    @State private var pressed = false
    @State private var start = Date()

    var body: some View {
        VStack(spacing: 14) {
            TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { timeline in
                let t = timeline.date.timeIntervalSince(start)
                let breath = CGFloat(max(0, sin(t * 2 * .pi / 5.0))) * 0.28
                CavnarSealMark(
                    size: 64,
                    ringColor: Color.cavnarInk3.opacity(0.32),
                    emberWarmth: pressed ? 1 : breath
                )
                .animation(.easeOut(duration: pressed ? 0.3 : 0.9), value: pressed)
            }
            .frame(width: 64, height: 64)
            .padding(.bottom, 4)

            Text(title)
                .font(.cavnarBody(15, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .multilineTextAlignment(.center)

            if let message {
                Text(message)
                    .font(.cavnarBody(14.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .multilineTextAlignment(.center)
                    .lineSpacing(3)
                    .frame(maxWidth: 300)
            }

            if let ctaLabel, let action {
                Button {
                    Haptic.light()
                    action()
                } label: {
                    Text(ctaLabel)
                }
                .buttonStyle(CavnarHearthButtonStyle { isPressed in pressed = isPressed })
                .padding(.top, 6)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 44)
        .padding(.horizontal, 24)
    }
}

/// Outlined ember CTA for the hearth — reports its press state up so the
/// seal above it can warm while the finger is down.
struct CavnarHearthButtonStyle: ButtonStyle {
    var onPress: (Bool) -> Void

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.cavnarBody(14, weight: 700))
            .foregroundStyle(configuration.isPressed ? Color.cavnarEmber2 : Color.cavnarEmber)
            .padding(.horizontal, 18)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .fill(Color.cavnarEmber.opacity(configuration.isPressed ? 0.16 : 0.08))
            )
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(Color.cavnarEmber.opacity(configuration.isPressed ? 0.9 : 0.5), lineWidth: 1.5)
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
            .onChange(of: configuration.isPressed) { _, isPressed in onPress(isPressed) }
    }
}

// MARK: - 15 · Pull-to-Refresh Ember

/// Replaces the system spinner on every .refreshable list with the one
/// shape the app already owns: an ember that stretches with the pull like a
/// drop of molten metal, snaps back, and flares once as the refresh fires.
/// The system UIRefreshControl still drives the gesture and the refresh
/// semantics — its own spinner is just tinted clear app-wide (see
/// CavnarAIApp) so only this shows. Pull distance comes from
/// onScrollGeometryChange on iOS 18+; on 17 the drop can't track the finger
/// and only appears for the refresh itself.
struct CavnarEmberRefreshable: ViewModifier {
    let action: () async -> Void

    @State private var pull: CGFloat = 0
    @State private var isRefreshing = false
    @State private var flareID = 0

    func body(content: Content) -> some View {
        Group {
            if #available(iOS 18.0, *) {
                content.onScrollGeometryChange(for: CGFloat.self) { geometry in
                    -(geometry.contentOffset.y + geometry.contentInsets.top)
                } action: { _, newValue in
                    pull = max(0, newValue)
                }
            } else {
                content
            }
        }
        .refreshable {
            flareID += 1
            isRefreshing = true
            await action()
            isRefreshing = false
        }
        .overlay(alignment: .top) {
            CavnarEmberPullIndicator(pull: pull, isRefreshing: isRefreshing, flareID: flareID)
                .allowsHitTesting(false)
        }
    }
}

extension View {
    func cavnarEmberRefreshable(_ action: @escaping () async -> Void) -> some View {
        modifier(CavnarEmberRefreshable(action: action))
    }
}

/// Rebuilt from scratch after the first version read as broken on a real
/// device: a ring sized exactly to the dot (12pt vs. the dot's own 12pt) sat
/// permanently around it at rest — meant only to appear as part of the
/// flare-out, but with no conditional rendering, it was just always there,
/// reading as a stray orange outline. And the anisotropic scale on the dot
/// (x shrinking while y grew, anchored at top, plus a separate capsule tail
/// above it) produced a tall, uneven oval instead of a clean drop. This
/// version is deliberately simpler: one circular dot with its own soft halo
/// that grows and fades in with the pull (isotropic scale only, so it always
/// stays round), breathes gently while refreshing, and gets exactly one
/// ripple — via CavnarRippleBurst, only ever mounted for the ~0.7s it plays —
/// on completion. Nothing persists in the tree at rest besides the dot itself.
struct CavnarEmberPullIndicator: View {
    var pull: CGFloat
    var isRefreshing: Bool
    var flareID: Int

    @State private var start = Date()
    @State private var showBurst = false

    private var pullProgress: CGFloat { min(pull / 70, 1) }
    private var visible: Bool { isRefreshing || pull > 4 }

    var body: some View {
        TimelineView(.animation(paused: !isRefreshing)) { timeline in
            let t = timeline.date.timeIntervalSince(start)
            let breathe: CGFloat = isRefreshing ? 1 + 0.16 * CGFloat(sin(t * 2 * .pi / 1.3)) : 1
            let scale = isRefreshing ? breathe : 0.5 + 0.5 * pullProgress
            let dotOpacity = isRefreshing ? 1.0 : Double(pullProgress)

            ZStack {
                Circle()
                    .fill(
                        RadialGradient(
                            colors: [Color.cavnarEmber.opacity(0.5), Color.cavnarEmber.opacity(0)],
                            center: .center, startRadius: 0, endRadius: 15
                        )
                    )
                    .frame(width: 30, height: 30)
                    .opacity(dotOpacity * 0.9)

                Circle()
                    .fill(
                        RadialGradient(
                            colors: [cavnarEmberHot, Color.cavnarEmber2, Color.cavnarEmber],
                            center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: 7
                        )
                    )
                    .frame(width: 13, height: 13)
                    .scaleEffect(scale)
                    .opacity(dotOpacity)
                    .shadow(color: Color.cavnarEmber.opacity(isRefreshing ? 0.85 : 0.5 * Double(pullProgress)), radius: isRefreshing ? 8 : 5)

                if showBurst {
                    CavnarRippleBurst(color: .cavnarEmber, fromDiameter: 13, toDiameter: 44, rings: 1, duration: 0.6)
                }
            }
        }
        .frame(height: 30)
        .offset(y: isRefreshing ? 16 : min(pull * 0.4, 24))
        .opacity(visible ? 1 : 0)
        .animation(.easeOut(duration: 0.2), value: isRefreshing)
        .onChange(of: flareID) { _, newValue in
            guard newValue > 0 else { return }
            showBurst = false
            DispatchQueue.main.async {
                showBurst = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.65) { showBurst = false }
            }
        }
    }
}
