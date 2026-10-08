import SwiftUI

/// The Ember Core — Cavnar AI's intelligence, drawn (10/8/26).
///
/// The same molten core as the web (`public/static/ember-core.js`, the
/// marketing site's hero; `static/ember-core.js`, the dashboard) — the shader
/// is ported line for line in `EmberCore.metal`. It is the AI itself, not a
/// loading indicator: it appears where the app means "this is Cavnar AI" and
/// is large and mostly at rest — the launch (the seal's ember ignites into
/// it), sign-in and the Face ID lock, Ask Cavnar AI before anything is
/// asked, and the whole-screen empty states (`CavnarEmptyHearth`, resting).
/// Working and loading stay with the dotted `CavnarOrb`.
///
/// States, as on the web: at rest it breathes and flows (nothing loops — the
/// flow is 3D noise, the breath two incommensurate sines); a touch leans the
/// heart toward the finger and warms it; a tap sends one ring of light out of
/// the shell; `thinking` quickens the flow and runs sparks along the veins;
/// `awake` (an empty state's action held down) brightens a resting core.
/// Reduce Motion draws one still frame.
///
/// Layout is the core's own diameter (`size`); it draws `scale` times that so
/// the glow and the orbiting embers have room, without taking that room in
/// layout or in hit testing.
struct EmberCoreView: View, Animatable {
    var size: CGFloat
    var scale: CGFloat = 2.6
    /// Resting energy: 0.55 is the core at its own pace; an empty state runs
    /// cooler (0.3).
    var energy: Double = 0.55
    var thinking: Bool = false
    var awake: Bool = false
    /// A caller-driven flare (the launch's ignition). Animatable.
    var flare: Double = 0
    var interactive: Bool = true

    nonisolated var animatableData: Double {
        get { flare }
        set { flare = newValue }
    }

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// Still under Reduce Motion, and in Low Power Mode or heat (parity #83):
    /// a 60fps Metal loop is ambient, never worth the battery.
    private var resting: Bool { reduceMotion || CavnarEnvironment.shared.reducedActivity }
    @Environment(\.displayScale) private var displayScale
    @State private var clock = EmberCoreClock()
    @State private var start = Date()

    private var pixels: CGFloat { size * displayScale }
    private var steps: Float {
        let px = pixels
        let s: Float = px >= 150 ? 10 : px >= 70 ? 7 : px >= 36 ? 5 : 3
        return min(s, 7)                      // a phone's GPU, as on the web
    }
    private var parts: Float { pixels >= 110 ? 12 : pixels >= 60 ? 9 : 0 }

    var body: some View {
        let side = size * scale
        TimelineView(.animation(minimumInterval: 1.0 / 60.0, paused: resting)) { timeline in
            let now = resting ? 12 : timeline.date.timeIntervalSince(start)
            let f = clock.step(now: now, thinking: thinking, awake: awake, base: energy,
                               parts: Int(parts), still: resting)
            Rectangle()
                .fill(Color.black)
                .frame(width: side, height: side)
                .colorEffect(ShaderLibrary.emberCore(
                    .float2(side, side), .float(f.t), .float(f.flow), .float(f.energy),
                    .float(f.pulse), .float(max(f.flare, flare)), .float(f.think),
                    .float(steps), .float(parts), .float(scale), .float(clock.seed),
                    .float2(f.gaze), .float(displayScale), .floatArray(f.embers)))
        }
        .frame(width: size, height: size)
        .contentShape(Circle())
        .allowsHitTesting(interactive)
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { value in
                    // the heart leans toward the finger
                    let r = max(size * 1.5, 80)
                    clock.gazeTarget = CGPoint(x: max(-1, min(1, (value.location.x - size / 2) / r)),
                                               y: max(-1, min(1, -(value.location.y - size / 2) / r)))
                    clock.touching = true
                }
                .onEnded { value in
                    clock.touching = false
                    clock.gazeTarget = .zero
                    if hypot(value.translation.width, value.translation.height) < 10 {
                        clock.tap()
                        Haptic.light()
                    }
                }
        )
        .accessibilityHidden(true)
    }
}

/// The core's running state, advanced once a frame from inside the
/// TimelineView. A reference type so the frame loop can integrate the flow
/// (its speed changes while thinking without the motion jumping) without a
/// state write per frame.
@MainActor
final class EmberCoreClock {
    struct Frame {
        var t: Double, flow: Double, energy: Double, pulse: Double, flare: Double, think: Double
        var gaze: CGPoint
        var embers: [Float]
    }

    let seed = Float.random(in: 1...40)
    var touching = false
    var gazeTarget = CGPoint.zero
    private var gaze = CGPoint.zero
    private var last: Double = -1
    private var flow: Double = 0
    private var energy: Double = 0.55
    private var think: Double = 0
    private var flare: Double = 0
    private var pulse: Double = -1
    private var embers = [Float](repeating: 0, count: 72)

    func tap() { pulse = 0; flare = 1 }

    func step(now: Double, thinking: Bool, awake: Bool, base: Double, parts: Int, still: Bool) -> Frame {
        let dt = last < 0 ? 1.0 / 60 : min(0.1, max(0, now - last))
        last = now
        let target = base + (touching ? 0.18 : 0) + 0.2 * think + (awake ? 0.45 : 0)
        energy += (min(1.2, target) - energy) * min(1, dt * 2.2)
        think += ((thinking ? 1 : 0) - think) * min(1, dt * 2.5)
        flare = max(0, flare - dt * 0.9)
        if pulse >= 0 { pulse += dt / 1.6; if pulse >= 1 { pulse = -1 } }
        let k = min(1, dt * 1.6)
        gaze = CGPoint(x: gaze.x + (gazeTarget.x - gaze.x) * k, y: gaze.y + (gazeTarget.y - gaze.y) * k)
        flow += still ? 0 : dt * (1 + 1.4 * think)
        let fl = still ? 12 : flow
        placeEmbers(parts, t: now, flow: fl)
        return Frame(t: now, flow: fl, energy: energy, pulse: pulse, flare: flare, think: think, gaze: gaze, embers: embers)
    }

    /// The embers' orbits, once a frame (as the web does): x, y, size, light.
    private func placeEmbers(_ n: Int, t: Double, flow: Double) {
        func hash(_ x: Double) -> Double { let v = sin(x) * 43758.5453; return v - floor(v) }
        let s = Double(seed)
        for k in 0..<min(n, 18) {
            let fk = Double(k)
            let h1 = hash(fk * 12.9898 + s), h2 = hash(fk * 78.233 + s), h3 = hash(fk * 39.42 + s)
            let orb = 1.1 + h1 * 0.75, sp = (0.035 + 0.09 * h2) * (k % 2 == 0 ? 1 : -1), a = h3 * 6.2831 + flow * sp
            let x = cos(a) * orb, y = sin(a) * orb * 0.16, z = sin(a) * orb
            let ax = (h1 - 0.5) * 1.1 + 0.25, y2 = cos(ax) * y - sin(ax) * z, z2 = sin(ax) * y + cos(ax) * z
            let ay = h2 * 3, x3 = cos(ay) * x + sin(ay) * z2, z3 = -sin(ay) * x + cos(ay) * z2
            let hidden: Double = (z3 < 0 && (x3 * x3 + y2 * y2).squareRoot() < 1) ? 0 : 1
            embers[k * 4] = Float(x3)
            embers[k * 4 + 1] = Float(y2)
            embers[k * 4 + 2] = Float((0.006 + 0.009 * h3) * (1 + 0.35 * z3 / orb))
            embers[k * 4 + 3] = Float(hidden * (0.5 + 0.5 * sin(t * (0.9 + h2 * 1.7) + fk * 2.1)))
        }
    }
}

#Preview {
    VStack(spacing: 60) {
        EmberCoreView(size: 120)
        EmberCoreView(size: 56, scale: 2.4, energy: 0.3)
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(Color.cavnarPaper)
}
