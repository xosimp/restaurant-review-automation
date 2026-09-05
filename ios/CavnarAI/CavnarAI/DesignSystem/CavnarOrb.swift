import SwiftUI

// MARK: - CavnarOrb
//
// The Ask Cavnar thinking orb: a point cloud on a sphere, projected in 3D
// and animated differently for each thing the agent is doing. Nine states,
// one engine — a native port of the same geometry the web client runs in
// static/cavnar-orb.js. The frame functions are pure math over
// (size, t, opts), so the two ports can be compared numerically and must
// stay in step; change one, change the other.
//
// Motion follows the app's rules (DesignSystem/CavnarMotion.swift): driven
// by TimelineView off the wall clock, never PhaseAnimator/repeatForever,
// so a tab switch or lock/unlock can't strand it mid-loop; `paused` is
// threaded through exactly like HomeObsidianField's, for callers covering
// it with a sheet. Reduced Motion gets one still frame of the current
// state rather than nothing — the state is still information.
//
// Branding: the reference orbs are grayscale. Ours paint on an ember ramp —
// far dots a muted warm grey, near dots ember — so the orb reads as ours
// without changing the motion. Ember is the only accent; nothing bounces.

/// What the agent is doing. Mirrors ask_cavnar.ORB_STATES on the backend,
/// which is where the SSE `state` field comes from.
enum CavnarOrbState: String, CaseIterable {
    case connecting   // stream opening, nothing has happened yet
    case solving      // the model deciding what to do
    case searching    // a read tool is running
    case working      // a direct action is executing
    case shaping      // a write proposal is being prepared for its confirm card
    case composing    // the final answer is being written
    case breathing    // idle — the header orb
    case listening    // reserved: voice input
    case weaving      // reserved: multi-tool synthesis

    /// Accessibility label, matching the reference library's wording.
    var label: String {
        switch self {
        case .connecting: return "Connecting"
        case .solving:    return "Solving"
        case .searching:  return "Searching"
        case .working:    return "Working"
        case .shaping:    return "Shaping"
        case .composing:  return "Composing"
        case .breathing:  return "Thinking"
        case .listening:  return "Listening"
        case .weaving:    return "Weaving"
        }
    }
}

struct CavnarOrb: View {
    var state: CavnarOrbState
    var size: CGFloat = 64
    /// Freezes the clock — for a presenting view covered by a sheet, where
    /// SwiftUI does not stop the TimelineView on its own.
    var paused: Bool = false
    /// Multiplies the animation clock. 1 is the tuned speed.
    var speed: Double = 1

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @State private var start = Date()

    var body: some View {
        let preset = CavnarOrbEngine.resolvePreset(state: state, size: size)
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: paused || reduceMotion)) { timeline in
            // Synchronous, not rendersAsynchronously: true — an async
            // Canvas commits its draw commands back to the main thread's
            // layer on its own schedule, decoupled from SwiftUI's own
            // frame timing. HomeObsidianField's three Canvas layers use
            // the plain synchronous form and Home doesn't show the
            // tab-bar glass-morph stall reported for this tab; this orb
            // was the one screen opted into async rendering. The paint
            // work here is a few hundred dots/lines — cheap enough
            // in-line — so this trades a theoretical async win for
            // removing an out-of-band commit landing at an unpredictable
            // moment relative to the tab bar's own transition.
            Canvas { context, canvasSize in
                let t = reduceMotion ? 0.6 : timeline.date.timeIntervalSince(start) * preset.speed * speed
                let frame = preset.frame(Double(size), t, preset.opts)
                CavnarOrbEngine.paint(frame, in: &context, dark: colorScheme == .dark)
            }
        }
        .frame(width: size, height: size)
        .accessibilityLabel(state.label)
        .accessibilityAddTraits(.isImage)
    }
}

// MARK: - Engine

enum CavnarOrbEngine {

    struct Dot {
        var x: Double, y: Double, z: Double, r: Double, white: Double, a: Double
        init(x: Double, y: Double, z: Double, r: Double, white: Double, a: Double = 1) {
            self.x = x; self.y = y; self.z = z; self.r = r; self.white = white; self.a = a
        }
    }
    struct Line {
        var x1: Double, y1: Double, x2: Double, y2: Double, white: Double, a: Double, w: Double
    }
    struct Frame {
        var dots: [Dot]
        var lines: [Line]
    }
    typealias Opts = [String: Double]
    typealias FrameFn = (_ size: Double, _ t: Double, _ opts: Opts) -> Frame

    struct Resolved {
        let mode: String
        let speed: Double
        let opts: Opts
        let frame: FrameFn
    }

    // MARK: Small math

    private static func lerp(_ a: Double, _ b: Double, _ t: Double) -> Double { a + (b - a) * t }
    private static func fract(_ x: Double) -> Double { x - floor(x) }
    private static func hash(_ n: Double, _ s: Double) -> Double {
        let t = sin(n * 12.9898 + s * 78.233) * 43758.5453
        return t - floor(t)
    }
    private static func noise(_ x: Double, _ y: Double) -> Double {
        let xi = floor(x), yi = floor(y)
        var fx = x - xi, fy = y - yi
        fx = fx * fx * (3 - 2 * fx)
        fy = fy * fy * (3 - 2 * fy)
        let a = hash(xi, yi), b = hash(xi + 1, yi), c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1)
        return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy
    }
    private static func fib(_ i: Int, _ n: Int) -> (Double, Double, Double) {
        let golden = Double.pi * (3 - sqrt(5.0))
        let y = 1 - 2 * (Double(i) + 0.5) / Double(n)
        let r = sqrt(max(0, 1 - y * y))
        let th = Double(i) * golden
        return (r * cos(th), y, r * sin(th))
    }
    private static func angDiff(_ a: Double, _ b: Double) -> Double { atan2(sin(a - b), cos(a - b)) }
    private static func smooth(_ x: Double) -> Double { x * x * (3 - 2 * x) }
    private static func opt(_ o: Opts, _ k: String, _ d: Double) -> Double { o[k] ?? d }

    /// Yaw around Y then tilt around X, then place at (cx, cy) with `scale`.
    private static func makeProj(_ yaw: Double, _ tilt: Double, _ cx: Double, _ cy: Double, _ scale: Double)
        -> (Double, Double, Double) -> (Double, Double, Double) {
        let st = sin(tilt), ct = cos(tilt), sy = sin(yaw), cyw = cos(yaw)
        return { x, y, z in
            let x1 = x * cyw + z * sy
            let z1 = -x * sy + z * cyw
            let y2 = y * ct - z1 * st
            let z2 = y * st + z1 * ct
            return (cx + x1 * scale, cy - y2 * scale, z2)
        }
    }
    private static func radiusScale(_ size: Double, _ pow_: Double) -> Double { pow(size / 300, pow_) }

    private static func finalize(_ dots: [Dot], _ lines: [Line], _ rMin: Double?) -> Frame {
        let min_ = rMin ?? 0.3
        var kept: [Dot] = []
        kept.reserveCapacity(dots.count)
        for var d in dots where d.a >= 0.02 {
            d.r = max(min_, d.r)
            kept.append(d)
        }
        kept.sort { $0.z < $1.z }
        return Frame(dots: kept, lines: lines.filter { $0.a >= 0.02 })
    }

    // MARK: Modes

    // Braid (weaving)
    private static func modeBraid(_ n: Double, _ s: Double, _ o: Opts) -> Frame {
        let cx = n / 2, cy = n / 2, R = n / 2 * 0.76
        let proj = makeProj(s * 0.4, 0.3, cx, cy, 1)
        let rs = radiusScale(n, opt(o, "rsPow", 0.6))
        var dots: [Dot] = []
        let ghostN = Int(opt(o, "ghostN", 150))
        for i in 0..<ghostN {
            let g = fib(i, ghostN)
            let p = proj(g.0 * R, g.1 * R, g.2 * R)
            let depth = (p.2 / R + 1) / 2
            dots.append(Dot(x: p.0, y: p.1, z: p.2, r: 0.8 * rs, white: 0.78, a: 0.1 + 0.22 * depth))
        }
        let strandN = Int(opt(o, "strandN", 52))
        let turns = opt(o, "turns", 3)
        for k in 0..<3 {
            let phase = Double(k) / 3 * 2 * .pi
            for i in 0..<strandN {
                let w = (fract(Double(i) / Double(strandN) + s * 0.045) * 2 - 1) * 0.96
                let ring = sqrt(max(0, 1 - w * w))
                let fade = min(1, (1 - abs(w)) / 0.1)
                let ang = w * .pi * turns + phase
                let bulge = 1 + 0.075 * sin(w * .pi * turns * 2 + phase * 2 + s * 0.8)
                let rr = ring * R * bulge
                let q = proj(cos(ang) * rr, w * R * bulge, sin(ang) * rr)
                let dd = (q.2 / R + 1) / 2
                dots.append(Dot(x: q.0, y: q.1, z: q.2,
                                r: (opt(o, "rBase", 1.2) + opt(o, "rDepth", 1.8) * dd) * rs,
                                white: 0.55 - 0.45 * dd, a: fade * (0.45 + 0.55 * dd)))
            }
        }
        return finalize(dots, [], o["rMin"])
    }

    // Rubik (solving)
    private struct RubikMove { let axis: Int; let lo: Double; let hi: Double; let ang: Double }
    private static func rubikSchedule(_ t: Double, _ count: Int, _ dur: Double, _ pause: Double)
        -> (amount: [Double], active: Int) {
        let period = 2 * Double(count) * dur + pause
        let u = t.truncatingRemainder(dividingBy: period)
        var amount = [Double](repeating: 0, count: count)
        var active = -1
        if u < 2 * Double(count) * dur {
            let idx = Int(floor(u / dur))
            let frac = (u - Double(idx) * dur) / dur
            let eased = 1 - pow(1 - min(1, frac / 0.7), 3)
            if idx < count {
                for i in 0..<idx { amount[i] = 1 }
                amount[idx] = eased
                active = idx
            } else {
                let back = 2 * count - 1 - idx
                for i in 0..<back { amount[i] = 1 }
                amount[back] = 1 - eased
                active = back
            }
        }
        return (amount, active)
    }
    private static func rubikMoves(_ count: Int) -> [RubikMove] {
        (0..<count).map { i in
            let di = Double(i)
            let axis = min(2, Int(floor(hash(di, 2.3) * 3)))
            let lo = -1 + 0.5 * Double(min(3, Int(floor(hash(di, 5.9) * 4))))
            let dir: Double = hash(di, 7.7) < 0.5 ? 1 : -1
            return RubikMove(axis: axis, lo: lo, hi: lo + 0.5, ang: dir * .pi / 2)
        }
    }
    private static func rubikApply(_ p: (Double, Double, Double), _ moves: [RubikMove],
                                   _ sched: (amount: [Double], active: Int)) -> (Double, Double, Double, Bool) {
        var (x, y, z) = p
        var hit = false
        for (i, m) in moves.enumerated() where sched.amount[i] > 0 {
            let coord = m.axis == 0 ? x : (m.axis == 1 ? y : z)
            if coord < m.lo || coord >= m.hi { continue }
            if i == sched.active { hit = true }
            let a = m.ang * sched.amount[i], c = cos(a), sn = sin(a)
            if m.axis == 0 { let tmp = y * c - z * sn; z = y * sn + z * c; y = tmp }
            else if m.axis == 1 { let tmp = x * c + z * sn; z = -x * sn + z * c; x = tmp }
            else { let tmp = x * c - y * sn; y = x * sn + y * c; x = tmp }
        }
        return (x, y, z, hit)
    }
    private static func modeRubik(_ n: Double, _ s: Double, _ o: Opts) -> Frame {
        let cx = n / 2, cy = n / 2, R = n / 2 * 0.82
        let proj = makeProj(s * 0.55, 0.35 + 0.1 * sin(s * 0.9), cx, cy, R)
        let rs = radiusScale(n, opt(o, "rsPow", 0.6))
        let moveCount = Int(opt(o, "moveCount", 14))
        let moves = rubikMoves(moveCount)
        let sched = rubikSchedule(s, moveCount, 0.42, 1.2)
        var dots: [Dot] = []
        let latRings = Int(opt(o, "latRings", 15))
        let lonDensity = opt(o, "lonDensity", 40)
        for i in 0...latRings {
            let lat = -Double.pi / 2 + Double(i) / Double(latRings) * .pi
            let cl = cos(lat), sl = sin(lat)
            let count = max(1, Int((abs(cl) * lonDensity).rounded()))
            for j in 0..<count {
                let lon = Double(j) / Double(count) * 2 * .pi
                let r3 = rubikApply((cl * cos(lon), sl, cl * sin(lon)), moves, sched)
                let p = proj(r3.0, r3.1, r3.2)
                let depth = (p.2 + 1) / 2
                dots.append(Dot(x: p.0, y: p.1, z: p.2,
                                r: (opt(o, "rBase", 0.6) + opt(o, "rDepth", 1.7) * depth
                                    + (r3.3 ? opt(o, "rActive", 0.3) : 0)) * rs,
                                white: opt(o, "inkFar", 0.62) - opt(o, "inkSpan", 0.54) * depth - (r3.3 ? 0.14 : 0)))
            }
        }
        return finalize(dots, [], o["rMin"])
    }

    // Globe (searching)
    private static func modeGlobe(_ n: Double, _ s: Double, _ o: Opts) -> Frame {
        let cx = n / 2, cy = n / 2, R = n / 2 * 0.82
        let tilt = 0.4 + 0.06 * sin(s * 0.35)
        let proj = makeProj(s * 0.5, tilt, cx, cy, R)
        let scan = s * (0.5 + (1.7 - 0.5) * opt(o, "scanMul", 1))
        let rs = radiusScale(n, opt(o, "rsPow", 0.6))
        let dimBase = opt(o, "dimBase", 1)
        var dots: [Dot] = []
        let latRings = Int(opt(o, "latRings", 17))
        let lonDensity = opt(o, "lonDensity", 44)
        for i in 0...latRings {
            let lat = -Double.pi / 2 + Double(i) / Double(latRings) * .pi
            let cl = cos(lat), sl = sin(lat)
            let count = max(1, Int((abs(cl) * lonDensity).rounded()))
            for j in 0..<count {
                let lon = Double(j) / Double(count) * 2 * .pi
                let p = proj(cl * cos(lon), sl, cl * sin(lon))
                let depth = (p.2 + 1) / 2
                let k = angDiff(lon + s * 0.5, scan)
                let glow = exp(-(k * k) / 0.18) * max(0, p.2)
                dots.append(Dot(x: p.0, y: p.1, z: p.2,
                                r: (opt(o, "rBase", 0.6) + opt(o, "rDepth", 1.7) * depth + opt(o, "rBoost", 1) * glow) * rs,
                                white: opt(o, "inkFar", 0.62) - opt(o, "inkSpan", 0.54) * depth,
                                a: dimBase + (1 - dimBase) * min(1, glow)))
            }
        }
        return finalize(dots, [], o["rMin"])
    }

    // Wave (listening)
    private static func modeWave(_ n: Double, _ s: Double, _ o: Opts) -> Frame {
        let cx = n / 2, cy = n / 2, R = n / 2 * 0.874
        let proj = makeProj(s * 0.18, 0.38, cx, cy, 1)
        let rs = radiusScale(n, opt(o, "rsPow", 0.6))
        var dots: [Dot] = []
        let rings = Int(opt(o, "rings", 15))
        let lonDensity = opt(o, "lonDensity", 40)
        for i in 0...rings {
            let lat = -Double.pi / 2 + Double(i) / Double(rings) * .pi
            let cl = cos(lat), sl = sin(lat)
            let w = 0.62 * sin(s * 2.1 - Double(i) * 0.52) + 0.38 * sin(s * 1.27 + Double(i) * 0.83)
            let rr = R * (0.88 + 0.105 * w)
            let count = max(1, Int((abs(cl) * lonDensity).rounded()))
            for j in 0..<count {
                let lon = Double(j) / Double(count) * 2 * .pi
                let p = proj(cl * cos(lon) * rr, sl * rr, cl * sin(lon) * rr)
                let depth = (p.2 / R + 1) / 2
                let lift = max(0, w)
                dots.append(Dot(x: p.0, y: p.1, z: p.2,
                                r: (opt(o, "rBase", 0.6) + opt(o, "rDepth", 1.7) * depth) * (1 + 0.4 * lift) * rs,
                                white: 0.66 - 0.56 * depth - 0.1 * lift))
            }
        }
        return finalize(dots, [], o["rMin"])
    }

    // Morph (shaping)
    private static func polyline(_ pts: [(Double, Double)]) -> (Double) -> (Double, Double) {
        let n = pts.count
        var lens: [Double] = []
        var total = 0.0
        for i in 0..<n {
            let a = pts[i], b = pts[(i + 1) % n]
            let L = hypot(b.0 - a.0, b.1 - a.1)
            lens.append(L); total += L
        }
        return { u in
            var d = u * total
            var k = 0
            while d > lens[k] && k < n - 1 { d -= lens[k]; k += 1 }
            let p = pts[k], q = pts[(k + 1) % n]
            let f = lens[k] > 0 ? min(1, d / lens[k]) : 0
            return (p.0 + (q.0 - p.0) * f, p.1 + (q.1 - p.1) * f)
        }
    }
    private static let shapeCircle: (Double) -> (Double, Double) = { u in
        let a = -Double.pi / 2 + u * 2 * .pi
        return (cos(a) * 0.24, sin(a) * 0.24)
    }
    private static let shapeTri = polyline([(0, -0.26), (0.24, 0.16), (-0.24, 0.16)])
    private static let shapeSquare = polyline([(0, -0.2), (0.2, -0.2), (0.2, 0.2), (-0.2, 0.2), (-0.2, -0.2)])
    private static let shapes: [(Double) -> (Double, Double)] = [shapeCircle, shapeTri, shapeSquare]
    private static let morphHold = 1.4, morphMove = 0.9
    private static func modeMorph(_ n: Double, _ s: Double, _ o: Opts) -> Frame {
        let cycle = morphHold + morphMove
        let cnt = shapes.count
        let u = s.truncatingRemainder(dividingBy: cycle * Double(cnt))
        let idx = Int(floor(u / cycle))
        let local = u - Double(idx) * cycle
        let mix = local > morphHold ? smooth((local - morphHold) / morphMove) : 0
        let spread = opt(o, "spread", 1)
        let from = shapes[idx], to = shapes[(idx + 1) % cnt]
        let samples = 160
        var path: [(Double, Double)] = []
        path.reserveCapacity(samples)
        for i in 0..<samples {
            let g = Double(i) / Double(samples)
            let a = from(g), b = to(g)
            path.append(((a.0 + (b.0 - a.0) * mix) * spread, (a.1 + (b.1 - a.1) * mix) * spread))
        }
        var lens: [Double] = []
        var total = 0.0
        for i in 0..<samples {
            let p = path[i], q = path[(i + 1) % samples]
            let L = hypot(q.0 - p.0, q.1 - p.1)
            lens.append(L); total += L
        }
        let dotN = max(6, Int((34 * opt(o, "iconD", 1)).rounded()))
        let rDot = opt(o, "rDot", 0.021) * 1.35 * spread
        let pulse = 1 + 0.02 * sin(local * 3.1)
        var dots: [Dot] = []
        let half = n / 2
        var seg = 0
        var acc = 0.0
        for i in 0..<dotN {
            let target = Double(i) / Double(dotN) * total
            while acc + lens[seg] < target && seg < samples - 1 { acc += lens[seg]; seg += 1 }
            let pa = path[seg], pb = path[(seg + 1) % samples]
            let f = lens[seg] > 0 ? min(1, (target - acc) / lens[seg]) : 0
            let X = (pa.0 + (pb.0 - pa.0) * f) * pulse
            let Y = (pa.1 + (pb.1 - pa.1) * f) * pulse
            dots.append(Dot(x: half + X * n, y: half + Y * n, z: 0, r: max(0.35, rDot * n), white: 0.1))
        }
        return finalize(dots, [], o["rMin"])
    }

    // Orbits (working)
    private static func modeOrbits(_ n: Double, _ s: Double, _ o: Opts) -> Frame {
        let cx = n / 2, cy = n / 2, R = n / 2 * 0.82
        let proj = makeProj(s * 0.12, 0.3, cx, cy, 1)
        let rs = radiusScale(n, opt(o, "rsPow", 0.6))
        var dots: [Dot] = []
        let orbitN = Int(opt(o, "orbitN", 12))
        let ghostN = Int(opt(o, "ghostN", 40))
        let particles = Int(opt(o, "particles", 3))
        for i in 0..<orbitN {
            let di = Double(i)
            let h1 = hash(di, 1.7), h2 = hash(di, 5.2), h3 = hash(di, 8.9)
            let rad = R * (0.45 + 0.52 * h1)
            let phi = h1 * 2 * .pi, theta = acos(2 * h2 - 1)
            let nx = sin(theta) * cos(phi), ny = cos(theta), nz = sin(theta) * sin(phi)
            var ux = -ny, uy = nx
            let uz = 0.0
            let ul = max(1e-6, sqrt(ux * ux + uy * uy))
            ux /= ul; uy /= ul
            let vx = ny * uz - nz * uy, vy = nz * ux - nx * uz, vz = nx * uy - ny * ux
            let speed = (0.25 + 0.55 * h3) * (h3 > 0.5 ? 1 : -1)
            for j in 0..<ghostN {
                let a = Double(j) / Double(ghostN) * 2 * .pi
                let p = proj((ux * cos(a) + vx * sin(a)) * rad,
                             (uy * cos(a) + vy * sin(a)) * rad,
                             (uz * cos(a) + vz * sin(a)) * rad)
                let d = (p.2 / rad + 1) / 2
                dots.append(Dot(x: p.0, y: p.1, z: p.2, r: opt(o, "ghostR", 0.9) * rs,
                                white: 0.72, a: opt(o, "ghostA", 0.5) * (0.4 + 0.6 * d)))
            }
            for j in 0..<particles {
                let b = s * speed + Double(j) / Double(particles) * 2 * .pi + h2 * 6
                let q = proj((ux * cos(b) + vx * sin(b)) * rad,
                             (uy * cos(b) + vy * sin(b)) * rad,
                             (uz * cos(b) + vz * sin(b)) * rad)
                let dd = (q.2 / rad + 1) / 2
                dots.append(Dot(x: q.0, y: q.1, z: q.2,
                                r: (opt(o, "partR", 1.2) + opt(o, "partRDepth", 1.6) * dd) * rs,
                                white: 0.3 - 0.22 * dd))
            }
        }
        return finalize(dots, [], o["rMin"])
    }

    // Ribbon (composing) / Ring (breathing) — `faceOn` is the ring variant.
    private static func modeRibbon(_ n: Double, _ s: Double, _ o: Opts) -> Frame {
        let cx = n / 2, cy = n / 2, R = n / 2 * 0.78
        let spin = opt(o, "spin", 1)
        let tilt = 0.3
        let proj = makeProj(s * 0.1 * spin, tilt, cx, cy, 1)
        let rs = radiusScale(n, opt(o, "rsPow", 0.6))
        let faceOn = opt(o, "faceOn", 0) != 0
        var dots: [Dot] = []
        let ghostN = Int(opt(o, "ghostN", 150))
        for i in 0..<ghostN {
            let g = fib(i, ghostN)
            let p = proj(g.0 * R, g.1 * R, g.2 * R)
            let d = (p.2 / R + 1) / 2
            dots.append(Dot(x: p.0, y: p.1, z: p.2, r: 0.8 * rs, white: 0.78, a: 0.1 + 0.22 * d))
        }
        let yaw = s * 0.24 * spin
        let lean = faceOn ? -tilt : 0.55 + 0.3 * sin(s * 0.18) * spin
        let cyw = cos(yaw), syw = sin(yaw)
        let e1x = cyw, e1y = 0.0, e1z = syw
        let e2x = -syw * sin(lean), e2y = cos(lean), e2z = cyw * sin(lean)
        let e3x = e1y * e2z - e1z * e2y, e3y = e1z * e2x - e1x * e2z, e3z = e1x * e2y - e1y * e2x
        let wobMul = opt(o, "wobMul", 1)
        let wob = 0.23 * wobMul
        let baseR = faceOn ? R / (1 + 0.85 * wob) : R
        let lanes = opt(o, "lanes", 5)
        let segs = Int(opt(o, "segs", 88))
        let bands = max(1, Int((lanes * opt(o, "bandMul", 1)).rounded()))
        for i in 0..<bands {
            let di = Double(i), db = Double(bands)
            let off = (di - (db - 1) / 2) * 0.075
            let edge = abs(di - (db - 1) / 2) / max(1, (db - 1) / 2)
            for j in 0..<segs {
                let a = Double(j) / Double(segs) * 2 * .pi
                let und = (0.16 * sin(a * 3 - s * 1.7 + di * 0.22) + 0.07 * sin(a * 5 + s * 1.1)) * wobMul
                let radMul = faceOn ? 1 + und : 1
                let lift = faceOn ? off : off + und
                let px = e1x * cos(a) + e2x * sin(a) + e3x * lift
                let py = e1y * cos(a) + e2y * sin(a) + e3y * lift
                let pz = e1z * cos(a) + e2z * sin(a) + e3z * lift
                let len = sqrt(px * px + py * py + pz * pz)
                let rr = baseR * radMul
                let q = proj(px / len * rr, py / len * rr, pz / len * rr)
                let dd = (q.2 / R + 1) / 2
                dots.append(Dot(x: q.0, y: q.1, z: q.2,
                                r: (opt(o, "rBase", 1.1) + opt(o, "rDepth", 1.7) * dd) * (1 - 0.25 * edge) * rs,
                                white: 0.52 - 0.44 * dd + 0.18 * edge, a: 0.4 + 0.6 * dd))
            }
        }
        return finalize(dots, [], o["rMin"])
    }

    // Web (connecting)
    private static func modeWeb(_ n: Double, _ s: Double, _ o: Opts) -> Frame {
        let cx = n / 2, cy = n / 2, R = n / 2 * 0.8 * opt(o, "spread", 1)
        let proj = makeProj(s * 0.12, 0.32, cx, cy, R)
        let rs = radiusScale(n, opt(o, "rsPow", 0.6))
        let nodeN = Int(opt(o, "nodeN", 30))
        let thr = opt(o, "thr", 0.72)
        let nodeR = opt(o, "nodeR", 1.4), nodeRDepth = opt(o, "nodeRDepth", 1.8)
        var nodes: [(Double, Double, Double)] = []
        nodes.reserveCapacity(nodeN)
        for i in 0..<nodeN {
            let f = fib(i, nodeN), di = Double(i)
            let x = f.0 + 0.3 * (noise(di * 0.31 + 9, s * 0.24) - 0.5) * 2
            let y = f.1 + 0.3 * (noise(di * 0.53 + 27, s * 0.21) - 0.5) * 2
            let z = f.2 + 0.3 * (noise(di * 0.77 + 55, s * 0.27) - 0.5) * 2
            let L = sqrt(x * x + y * y + z * z)
            nodes.append((x / L, y / L, z / L))
        }
        var lines: [Line] = []
        var dots: [Dot] = []
        for i in 0..<nodeN {
            for j in (i + 1)..<nodeN {
                let dx = nodes[i].0 - nodes[j].0, dy = nodes[i].1 - nodes[j].1, dz = nodes[i].2 - nodes[j].2
                let dist = sqrt(dx * dx + dy * dy + dz * dz)
                if dist >= thr { continue }
                let a = proj(nodes[i].0, nodes[i].1, nodes[i].2)
                let b = proj(nodes[j].0, nodes[j].1, nodes[j].2)
                let zm = ((a.2 + b.2) / 2 + 1) / 2
                lines.append(Line(x1: a.0, y1: a.1, x2: b.0, y2: b.1, white: 0.42,
                                  a: (1 - dist / thr) * (0.3 + 0.55 * zm),
                                  w: max(0.6, opt(o, "lineW", 0.8) * rs)))
            }
        }
        for i in 0..<nodeN {
            let p = proj(nodes[i].0, nodes[i].1, nodes[i].2)
            let d = (p.2 + 1) / 2
            let pulse = 1 + 0.25 * sin(s * 1.4 + Double(i) * 2.7)
            dots.append(Dot(x: p.0, y: p.1, z: p.2, r: (nodeR + nodeRDepth * d) * pulse * rs, white: 0.55 - 0.45 * d))
        }
        let signals = Int(opt(o, "signals", 5))
        for i in 0..<signals {
            let di = Double(i)
            let tick = floor(s * 0.55 + di * 7.31)
            let from = Int(floor(hash(tick, di * 3.1 + 1.7) * Double(nodeN)))
            let to = Int(floor(hash(tick, di * 5.7 + 4.2) * Double(nodeN)))
            if from == to { continue }
            let u = fract(s * 0.55 + di * 7.31)
            let sx = lerp(nodes[from].0, nodes[to].0, u)
            let sy = lerp(nodes[from].1, nodes[to].1, u)
            let sz = lerp(nodes[from].2, nodes[to].2, u)
            let sl = max(1e-6, sqrt(sx * sx + sy * sy + sz * sz))
            let q = proj(sx / sl, sy / sl, sz / sl)
            let qd = (q.2 + 1) / 2
            dots.append(Dot(x: q.0, y: q.1, z: q.2, r: (nodeR * 1.5 + nodeRDepth * qd) * rs, white: 0.05, a: 0.5 + 0.5 * qd))
        }
        return finalize(dots, lines, o["rMin"])
    }

    private static let modeFrames: [String: FrameFn] = [
        "orbits": modeOrbits, "globe": modeGlobe, "rubik": modeRubik, "wave": modeWave,
        "web": modeWeb, "braid": modeBraid, "ribbon": modeRibbon, "ring": modeRibbon, "morph": modeMorph,
    ]

    // MARK: Presets — must match static/cavnar-orb.js exactly

    private static let base: [String: Opts] = [
        "globe":  ["latRings": 17, "lonDensity": 44, "rBase": 0.6, "rDepth": 1.7, "rBoost": 1, "inkFar": 0.62, "inkSpan": 0.54, "rsPow": 0.6, "rMin": 0.3],
        "orbits": ["orbitN": 12, "ghostN": 40, "ghostR": 0.9, "ghostA": 0.5, "particles": 3, "partR": 1.2, "partRDepth": 1.6, "rsPow": 0.6, "rMin": 0.3],
        "rubik":  ["latRings": 15, "lonDensity": 40, "moveCount": 14, "rBase": 0.6, "rDepth": 1.7, "rActive": 0.3, "inkFar": 0.62, "inkSpan": 0.54, "rsPow": 0.6, "rMin": 0.3],
        "wave":   ["rings": 15, "lonDensity": 40, "rBase": 0.6, "rDepth": 1.7, "rsPow": 0.6, "rMin": 0.3],
        "web":    ["nodeN": 30, "thr": 0.72, "signals": 5, "nodeR": 1.4, "nodeRDepth": 1.8, "lineW": 0.8, "rsPow": 0.6, "rMin": 0.3],
        "braid":  ["strandN": 52, "turns": 3, "ghostN": 150, "rBase": 1.2, "rDepth": 1.8, "rsPow": 0.6, "rMin": 0.3],
        "ribbon": ["lanes": 5, "segs": 88, "ghostN": 150, "rBase": 1.1, "rDepth": 1.7, "rsPow": 0.6, "rMin": 0.3],
        "ring":   ["lanes": 5, "segs": 88, "ghostN": 0, "faceOn": 1, "rBase": 1.1, "rDepth": 1.7, "rsPow": 0.6, "rMin": 0.3],
        "morph":  ["rDot": 0.021, "iconD": 1, "rMin": 0.25],
    ]

    private static let stateToMode: [CavnarOrbState: String] = [
        .working: "orbits", .searching: "globe", .solving: "rubik", .listening: "wave",
        .connecting: "web", .weaving: "braid", .composing: "ribbon", .breathing: "ring", .shaping: "morph",
    ]

    private struct Preset { let speed: Double; let count: Double; let size: Double; let extra: Opts }
    private static let presets: [String: [Int: Preset]] = [
        "orbits": [64: Preset(speed: 1.885, count: 1, size: 1, extra: [:]),
                   20: Preset(speed: 3.9, count: 0.238, size: 2.4, extra: [:])],
        "globe":  [64: Preset(speed: 2.015, count: 0.42, size: 1.15, extra: ["scanMul": 4.08, "dimBase": 0.45]),
                   20: Preset(speed: 2.665, count: 0.105, size: 1.75, extra: ["scanMul": 4.335, "dimBase": 0.45])],
        "rubik":  [64: Preset(speed: 1.82, count: 0.35, size: 1.05, extra: [:]),
                   20: Preset(speed: 1.95, count: 0.088, size: 1.9, extra: [:])],
        "wave":   [64: Preset(speed: 4.388, count: 0.341, size: 1, extra: [:]),
                   20: Preset(speed: 3.998, count: 0.105, size: 1.6, extra: [:])],
        "web":    [64: Preset(speed: 3.315, count: 1.35, size: 0.95, extra: [:]),
                   20: Preset(speed: 6.63, count: 0.25, size: 1.52, extra: [:])],
        "braid":  [64: Preset(speed: 1.625, count: 0.5, size: 1, extra: [:]),
                   20: Preset(speed: 2.75, count: 0.1125, size: 1.36, extra: [:])],
        "ribbon": [64: Preset(speed: 2.34, count: 0.25, size: 0.85, extra: ["spin": 0, "bandMul": 3.9, "wobMul": 1]),
                   20: Preset(speed: 3.12, count: 0.051, size: 1.073, extra: ["spin": 0, "bandMul": 4.94, "wobMul": 1])],
        "ring":   [64: Preset(speed: 3.24, count: 0.25, size: 0.956, extra: ["spin": 0, "bandMul": 3.627, "wobMul": 0.368]),
                   20: Preset(speed: 3.78, count: 0.028, size: 1.622, extra: ["spin": 0, "bandMul": 3.968, "wobMul": 0.565])],
        "morph":  [64: Preset(speed: 2.405, count: 0.702, size: 0.395, extra: ["spread": 1.45]),
                   20: Preset(speed: 2.08, count: 0.53, size: 1.011, extra: ["spread": 1.45])],
    ]

    private static let paired = [("latRings", "lonDensity"), ("rings", "lonDensity"), ("lanes", "segs")]
    private static let counts = ["orbitN", "ghostN", "nodeN", "strandN", "signals"]
    private static let icons = ["iconD"]
    private static let radii = ["rBase", "rDepth", "rActive", "rDot", "ghostR", "partR", "partRDepth", "nodeR", "nodeRDepth"]

    private static func scaleCounts(_ opts: Opts, _ scale: Double) -> Opts {
        var t = opts
        var done = Set<String>()
        let sq = sqrt(scale)
        for (a, b) in paired {
            if let va = t[a], let vb = t[b], !done.contains(a), !done.contains(b) {
                t[a] = max(2, (va * sq).rounded())
                t[b] = max(2, (vb * sq).rounded())
                done.insert(a); done.insert(b)
            }
        }
        for k in counts {
            if let v = t[k], v != 0, !done.contains(k) { t[k] = max(1, (v * scale).rounded()) }
        }
        for k in icons {
            if let v = t[k] { t[k] = max(0.02, v * scale) }
        }
        return t
    }
    private static func scaleRadii(_ opts: Opts, _ scale: Double) -> Opts {
        var t = opts
        for k in radii { if let v = t[k] { t[k] = v * scale } }
        t["rSizeMul"] = (t["rSizeMul"] ?? 1) * scale
        return t
    }

    private static var presetCache: [String: Resolved] = [:]
    private static let cacheLock = NSLock()

    /// Resolve a state at a rendered size. Two tunings exist — "64" for
    /// chat-avatar scale and "20" for inline-text scale — chosen by which
    /// the requested size is nearer to; radius scaling then adapts to the
    /// exact size continuously.
    static func resolvePreset(state: CavnarOrbState, size: CGFloat) -> Resolved {
        let sizeKey = size >= 40 ? 64 : 20
        let key = "\(state.rawValue)-\(sizeKey)"
        cacheLock.lock(); defer { cacheLock.unlock() }
        if let cached = presetCache[key] { return cached }
        let mode = stateToMode[state] ?? "orbits"
        let p = presets[mode]![sizeKey]!
        var opts = base[mode]!
        if p.count != 1 { opts = scaleCounts(opts, p.count) }
        if p.size != 1 { opts = scaleRadii(opts, p.size) }
        for (k, v) in p.extra { opts[k] = v }
        let resolved = Resolved(mode: mode, speed: p.speed, opts: opts, frame: modeFrames[mode]!)
        presetCache[key] = resolved
        return resolved
    }

    // MARK: Painting

    /// The ember ramp. `white` (0..1, higher = fainter) becomes a blend from
    /// a muted warm grey out to ember. Same endpoints as the web port, with
    /// the iOS Ember token (#D4583A) for light mode.
    private static func ink(_ white: Double, dark: Bool) -> Color {
        let strength = 1 - min(1, max(0, white))
        let near: (Double, Double, Double) = dark ? (232, 149, 106) : (212, 88, 58)   // Ember2 / Ember
        let far: (Double, Double, Double) = dark ? (107, 90, 82) : (184, 173, 164)    // warm greys
        let r = far.0 + (near.0 - far.0) * strength
        let g = far.1 + (near.1 - far.1) * strength
        let b = far.2 + (near.2 - far.2) * strength
        return Color(red: r / 255, green: g / 255, blue: b / 255)
    }

    static func paint(_ frame: Frame, in context: inout GraphicsContext, dark: Bool) {
        for l in frame.lines {
            var path = Path()
            path.move(to: CGPoint(x: l.x1, y: l.y1))
            path.addLine(to: CGPoint(x: l.x2, y: l.y2))
            context.stroke(path, with: .color(ink(l.white, dark: dark).opacity(l.a)), lineWidth: l.w)
        }
        for d in frame.dots {
            let rect = CGRect(x: d.x - d.r, y: d.y - d.r, width: d.r * 2, height: d.r * 2)
            context.fill(Path(ellipseIn: rect), with: .color(ink(d.white, dark: dark).opacity(d.a)))
        }
    }
}

#Preview("All states") {
    ScrollView {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 24) {
            ForEach(CavnarOrbState.allCases, id: \.self) { s in
                VStack(spacing: 8) {
                    CavnarOrb(state: s, size: 64)
                    Text(s.label).font(.caption)
                }
            }
        }
        .padding()
    }
    .background(Color.cavnarPaper)
}
