import SwiftUI

/// Restaurant DNA, told (owner, 10/8/26 — the redesign; web: the `dn-` page
/// at /dna, DESIGN_SYSTEM.md → Restaurant DNA). `story` rides beside the
/// profile on /mobile/api/dna (intelligence/dna_story.py): the read, the
/// traits each EARNED by a measured figure against a stated rule, the shape
/// (50 = the stated benchmark, nil = still learning — never 0), what moves
/// together and what is still learning. Nothing here is computed on the
/// phone; it draws what the server measured.

extension RestaurantDNA {
    struct Story: Decodable {
        struct Identity: Decodable, Identifiable {
            let key: String
            let label: String
            var id: String { key }
        }

        struct Trait: Decodable, Identifiable {
            let key: String
            let name: String
            let icon: String
            let family: String
            let figure: String
            let figureLabel: String
            let sentence: String
            let rule: String
            let dims: [String]
            let strength: Int?
            let tone: String?
            var id: String { key }
            var isWatch: Bool { tone == "watch" }
            enum CodingKeys: String, CodingKey {
                case key, name, icon, family, figure, sentence, rule, dims, strength, tone
                case figureLabel = "figure_label"
            }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                key = try c.decode(String.self, forKey: .key)
                name = ((try? c.decodeIfPresent(String.self, forKey: .name)) ?? nil) ?? key
                icon = ((try? c.decodeIfPresent(String.self, forKey: .icon)) ?? nil) ?? "spark"
                family = ((try? c.decodeIfPresent(String.self, forKey: .family)) ?? nil) ?? ""
                figure = ((try? c.decodeIfPresent(String.self, forKey: .figure)) ?? nil) ?? ""
                figureLabel = ((try? c.decodeIfPresent(String.self, forKey: .figureLabel)) ?? nil) ?? ""
                sentence = ((try? c.decodeIfPresent(String.self, forKey: .sentence)) ?? nil) ?? ""
                rule = ((try? c.decodeIfPresent(String.self, forKey: .rule)) ?? nil) ?? ""
                dims = ((try? c.decodeIfPresent([String].self, forKey: .dims)) ?? nil) ?? []
                strength = (try? c.decodeIfPresent(Int.self, forKey: .strength)) ?? nil
                tone = (try? c.decodeIfPresent(String.self, forKey: .tone)) ?? nil
            }
        }

        struct Progress: Decodable {
            let have: Int
            let need: Int
            let unit: String
            let pct: Int
        }

        struct Learning: Decodable, Identifiable {
            let key: String
            let name: String
            let icon: String
            let needs: String?
            let dormant: Bool
            let progress: Progress?
            var id: String { key }
            enum CodingKeys: String, CodingKey { case key, name, icon, needs, dormant, progress }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                key = try c.decode(String.self, forKey: .key)
                name = ((try? c.decodeIfPresent(String.self, forKey: .name)) ?? nil) ?? key
                icon = ((try? c.decodeIfPresent(String.self, forKey: .icon)) ?? nil) ?? "spark"
                needs = (try? c.decodeIfPresent(String.self, forKey: .needs)) ?? nil
                dormant = ((try? c.decodeIfPresent(Bool.self, forKey: .dormant)) ?? nil) ?? false
                progress = (try? c.decodeIfPresent(Progress.self, forKey: .progress)) ?? nil
            }
            var line: String {
                if let p = progress { return "\(p.have) of \(p.need) \(p.unit)" }
                if dormant { return "Not measurable on Cavnar AI yet \u{2014} needs " + (needs ?? "a source") }
                return "Needs " + (needs ?? "more data")
            }
        }

        struct Axis: Decodable, Identifiable {
            let key: String
            let label: String
            let score: Int?
            let measured: Int
            let of: Int
            var id: String { key }
            enum CodingKeys: String, CodingKey { case key, label, score, measured, of }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                key = try c.decode(String.self, forKey: .key)
                label = ((try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil) ?? key
                score = (try? c.decodeIfPresent(Int.self, forKey: .score)) ?? nil
                measured = ((try? c.decodeIfPresent(Int.self, forKey: .measured)) ?? nil) ?? 0
                of = ((try? c.decodeIfPresent(Int.self, forKey: .of)) ?? nil) ?? 0
            }
        }

        struct Node: Decodable, Identifiable {
            let label: String
            let figure: String?
            let note: String?
            var id: String { label }
        }

        struct Connection: Decodable, Identifiable {
            let key: String
            let title: String
            let nodes: [Node]
            let sentence: String
            var id: String { key }
        }

        struct Observed: Decodable {
            let nights: Int?
            let since: String?
            let measured: Int?
            let of: Int?
            let coveragePct: Int?
            let learning: Int?
            enum CodingKeys: String, CodingKey {
                case nights, since, measured, of, learning
                case coveragePct = "coverage_pct"
            }
        }

        let headline: String?
        let lede: String?
        let identity: [Identity]
        let traits: [Trait]
        let learning: [Learning]
        let axes: [Axis]
        let connections: [Connection]
        let observed: Observed?
        let basis: String?

        enum CodingKeys: String, CodingKey { case headline, lede, identity, traits, learning, axes, connections, observed, basis }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            headline = (try? c.decodeIfPresent(String.self, forKey: .headline)) ?? nil
            lede = (try? c.decodeIfPresent(String.self, forKey: .lede)) ?? nil
            identity = (try? c.decodeIfPresent(HomeLenientListDecodable<Identity>.self, forKey: .identity))?.items ?? []
            traits = (try? c.decodeIfPresent(HomeLenientListDecodable<Trait>.self, forKey: .traits))?.items ?? []
            learning = (try? c.decodeIfPresent(HomeLenientListDecodable<Learning>.self, forKey: .learning))?.items ?? []
            axes = (try? c.decodeIfPresent(HomeLenientListDecodable<Axis>.self, forKey: .axes))?.items ?? []
            connections = (try? c.decodeIfPresent(HomeLenientListDecodable<Connection>.self, forKey: .connections))?.items ?? []
            observed = (try? c.decodeIfPresent(Observed.self, forKey: .observed)) ?? nil
            basis = (try? c.decodeIfPresent(String.self, forKey: .basis)) ?? nil
        }

        /// The read's first sentence (the headline) and the rest.
        var headParts: (head: String, rest: String) {
            let h = headline ?? ""
            guard let r = h.range(of: ". ") else { return (h, "") }
            return (String(h[..<r.lowerBound]) + ".", String(h[r.upperBound...]))
        }

        /// Traits in the order a reader meets them: the measured strengths
        /// first, a watch after them.
        var orderedTraits: [Trait] { traits.filter { !$0.isWatch } + traits.filter(\.isWatch) }
    }
}

/// The SF Symbol for a story icon key (the web draws its own strokes).
enum DNAIcon {
    static func symbol(_ key: String) -> String {
        switch key {
        case "calendar": return "calendar"
        case "sun": return "sun.max"
        case "moon": return "moon"
        case "wave": return "waveform.path"
        case "target": return "scope"
        case "leaf": return "leaf"
        case "trend_up": return "chart.line.uptrend.xyaxis"
        case "trend_down": return "chart.line.downtrend.xyaxis"
        case "clock": return "clock"
        case "people": return "person.2"
        case "shield": return "checkmark.shield"
        case "flex": return "chart.bar"
        case "star": return "star"
        case "reply": return "arrowshape.turn.up.left"
        case "register": return "banknote"
        case "glass": return "wineglass"
        case "heart": return "heart"
        default: return "sparkles"
        }
    }
}

// MARK: - Small pieces

struct DNAChip: View {
    let label: String
    var icon: String? = nil
    var earned = false
    var watch = false

    var body: some View {
        HStack(spacing: 6) {
            if let icon {
                Image(systemName: DNAIcon.symbol(icon))
                    .font(.cavnar(.caption))
                    .foregroundStyle(watch ? Color.cavnarAmber : Color.cavnarEmber2)
                    .accessibilityHidden(true)
            }
            Text(label)
                .cavnarText(.caption, color: earned ? .cavnarInk : .cavnarInk2)
                .lineLimit(2)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 5)
        // Grows with the text (L10): a fixed 30pt chip clipped a long trait.
        .frame(minHeight: 30)
        .background(
            Capsule().fill(watch ? Color.cavnarAmber.opacity(0.1)
                           : (earned ? Color.cavnarEmber.opacity(0.13) : Color.white.opacity(0.03)))
        )
        .overlay(
            Capsule().strokeBorder(watch ? Color.cavnarAmber.opacity(0.45)
                                   : (earned ? Color.cavnarEmber.opacity(0.45) : Color.cavnarInk3.opacity(0.25)), lineWidth: 1)
        )
    }
}

/// A ring that fills to `fraction` once it appears.
struct DNARing: View {
    let fraction: Double
    var size: CGFloat = 64
    var label: String? = nil
    var caption: String? = nil
    var tint: Color = .cavnarEmber
    @State private var shown = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            Circle().stroke(Color.white.opacity(0.08), lineWidth: size > 60 ? 6 : 4.5)
            Circle()
                .trim(from: 0, to: shown ? max(0, min(1, fraction)) : 0)
                .stroke(tint, style: StrokeStyle(lineWidth: size > 60 ? 6 : 4.5, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .shadow(color: tint.opacity(0.55), radius: 5)
            VStack(spacing: 0) {
                if let label {
                    Text(label)
                        .font(.cavnarNumber(size * 0.22, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .minimumScaleFactor(0.85)
                        .lineLimit(1)
                }
                if let caption {
                    Text(caption)
                        .font(.cavnarBody(max(9, size * 0.12), weight: 500))
                        .foregroundStyle(Color.cavnarInk2)
                }
            }
            .padding(6)
        }
        .frame(width: size, height: size)
        .onAppear {
            if reduceMotion { shown = true } else { withAnimation(.easeOut(duration: 1.3).delay(0.15)) { shown = true } }
        }
    }
}

/// A horizontal track with the figure filled and the typical value ticked.
struct DNAGauge: View {
    let lo: Double
    let hi: Double
    let value: Double
    let typical: Double?
    let format: (Double) -> String
    var label: String? = nil
    var watch = false
    @State private var shown = false

    private func frac(_ v: Double) -> CGFloat { CGFloat(max(0, min(1, (v - lo) / max(0.0001, hi - lo)))) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let label {
                HStack {
                    Text(label).cavnarText(.caption, color: .cavnarInk2)
                    Spacer()
                    HomeMixedText.make(format(value), role: .caption, color: .cavnarInk)
                }
            }
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.07))
                    Capsule()
                        .fill(LinearGradient(colors: watch ? [Color.cavnarAmber.opacity(0.6), .cavnarAmber]
                                                           : [.cavnarEmber, .cavnarEmber2],
                                             startPoint: .leading, endPoint: .trailing))
                        .frame(width: shown ? g.size.width * frac(value) : 0)
                    if let typical {
                        RoundedRectangle(cornerRadius: 1)
                            .fill(Color.cavnarInk.opacity(0.55))
                            .frame(width: 2, height: 18)
                            .offset(x: g.size.width * frac(typical) - 1)
                    }
                }
            }
            .frame(height: 10)
            HStack {
                Text(format(lo))
                Spacer()
                if let typical { Text("typical " + format(typical)) }
                Spacer()
                Text(format(hi))
            }
            .cavnarText(.caption, color: .cavnarInk2)
        }
        .onAppear { withAnimation(.easeOut(duration: 1.2).delay(0.1)) { shown = true } }
    }
}

/// Monday to Thursday against Friday to Sunday, the typical weekend ticked.
struct DNASplitBar: View {
    let weekendShare: Double
    @State private var shown = false

    var body: some View {
        let b = Int((weekendShare * 100).rounded()), a = 100 - b
        VStack(alignment: .leading, spacing: 6) {
            GeometryReader { g in
                let w = g.size.width, split = shown ? CGFloat(a) / 100 : 0.5
                ZStack(alignment: .leading) {
                    HStack(spacing: 0) {
                        Text("Mon\u{2013}Thu \(a)%")
                            .cavnarText(.caption, color: .cavnarInk2)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                            .padding(.leading, 10)
                            .frame(width: w * split, height: 34, alignment: .leading)
                            .background(Color.white.opacity(0.07))
                        Text("Fri\u{2013}Sun \(b)%")
                            .cavnarText(.caption, color: .cavnarPaper)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                            .padding(.trailing, 10)
                            .frame(width: w * (1 - split), height: 34, alignment: .trailing)
                            .background(LinearGradient(colors: [.cavnarEmber, .cavnarEmber2], startPoint: .leading, endPoint: .trailing))
                    }
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    Rectangle().fill(Color.cavnarInk.opacity(0.5)).frame(width: 2, height: 40).offset(x: w * 0.55 - 1)
                }
            }
            .frame(height: 34)
            HStack {
                Text("Mon to Thu")
                Spacer()
                Text("typical weekend 45%")
                Spacer()
                Text("Fri to Sun")
            }
            .cavnarText(.caption, color: .cavnarInk2)
        }
        .onAppear { withAnimation(.cavnarEase(1.1).delay(0.1)) { shown = true } }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(b)% of sales Friday to Sunday; the stated benchmark is 45%")
    }
}

struct DNAStars: View {
    let rating: Double
    var body: some View {
        HStack(spacing: 6) {
            ForEach(0..<5, id: \.self) { i in
                let f = max(0, min(1, rating - Double(i)))
                ZStack(alignment: .leading) {
                    Image(systemName: "star.fill").foregroundStyle(Color.white.opacity(0.08))
                    Image(systemName: "star.fill").foregroundStyle(Color.cavnarEmber2)
                        .mask(alignment: .leading) { Rectangle().frame(width: 26 * f) }
                }
                .font(.system(size: 24))
                .frame(width: 26, height: 26)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(String(format: "%.2f of 5 stars", rating))
    }
}

// MARK: - The shape

struct DNARadar: View {
    let axes: [RestaurantDNA.Story.Axis]
    var mini = false
    @State private var grown = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private func point(_ i: Int, _ r: CGFloat, _ c: CGPoint) -> CGPoint {
        let a = -Double.pi / 2 + Double(i) * 2 * .pi / Double(max(1, axes.count))
        return CGPoint(x: c.x + CGFloat(cos(a)) * r, y: c.y + CGFloat(sin(a)) * r)
    }

    private func ring(_ f: CGFloat, _ R: CGFloat, _ c: CGPoint) -> Path {
        var p = Path()
        for i in axes.indices {
            let q = point(i, R * f, c)
            if i == 0 { p.move(to: q) } else { p.addLine(to: q) }
        }
        p.closeSubpath()
        return p
    }

    var body: some View {
        GeometryReader { g in
            let side = min(g.size.width, g.size.height)
            let c = CGPoint(x: g.size.width / 2, y: g.size.height / 2)
            let R = side * (mini ? 0.44 : 0.34)
            ZStack {
                ForEach([1.0, 0.75, 0.25], id: \.self) { f in
                    ring(CGFloat(f), R, c).stroke(Color.cavnarInk3.opacity(0.14), lineWidth: 1)
                }
                ring(0.5, R, c).stroke(Color.cavnarEmber2.opacity(0.55), style: StrokeStyle(lineWidth: 1.2, dash: [4, 6]))
                ForEach(axes.indices, id: \.self) { i in
                    Path { p in p.move(to: c); p.addLine(to: point(i, R, c)) }
                        .stroke(Color.cavnarInk3.opacity(0.16), style: StrokeStyle(lineWidth: 1, dash: axes[i].score == nil ? [3, 5] : []))
                }
                let measured = axes.indices.filter { axes[$0].score != nil }
                Group {
                    if measured.count >= 3 {
                        Path { p in
                            for (n, i) in measured.enumerated() {
                                let q = point(i, R * max(0.04, CGFloat(axes[i].score ?? 0) / 100), c)
                                if n == 0 { p.move(to: q) } else { p.addLine(to: q) }
                            }
                            p.closeSubpath()
                        }
                        .fill(RadialGradient(colors: [Color.cavnarEmber.opacity(0.55), Color.cavnarEmber2.opacity(0.12)],
                                             center: .center, startRadius: 0, endRadius: R))
                        .overlay(
                            Path { p in
                                for (n, i) in measured.enumerated() {
                                    let q = point(i, R * max(0.04, CGFloat(axes[i].score ?? 0) / 100), c)
                                    if n == 0 { p.move(to: q) } else { p.addLine(to: q) }
                                }
                                p.closeSubpath()
                            }
                            .stroke(Color.cavnarEmber, style: StrokeStyle(lineWidth: mini ? 1.5 : 2, lineJoin: .round))
                        )
                    }
                    ForEach(measured, id: \.self) { i in
                        let q = point(i, R * max(0.04, CGFloat(axes[i].score ?? 0) / 100), c)
                        Circle().fill(Color.cavnarEmber2).frame(width: mini ? 5 : 8, height: mini ? 5 : 8).position(q)
                    }
                }
                .scaleEffect(grown ? 1 : 0.15, anchor: UnitPoint(x: c.x / max(1, g.size.width), y: c.y / max(1, g.size.height)))
                .opacity(grown ? 1 : 0)
                ForEach(axes.indices.filter { axes[$0].score == nil }, id: \.self) { i in
                    Circle().strokeBorder(Color.cavnarInk3, style: StrokeStyle(lineWidth: 1, dash: [2, 3]))
                        .frame(width: mini ? 4 : 7, height: mini ? 4 : 7)
                        .position(point(i, R * 0.5, c))
                }
                if !mini {
                    ForEach(axes.indices, id: \.self) { i in
                        let q = point(i, R + 30, c)
                        VStack(spacing: 1) {
                            Text(axes[i].label)
                                .cavnarText(.caption, color: .cavnarInk2)
                                .lineLimit(2)
                                .minimumScaleFactor(0.85)
                            HomeMixedText.make(axes[i].score.map(String.init) ?? "learning", role: .caption,
                                               color: axes[i].score == nil ? .cavnarInk2 : .cavnarInk)
                        }
                        .multilineTextAlignment(.center)
                        .frame(width: 92)
                        .position(q)
                    }
                }
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .onAppear {
            if reduceMotion || mini { grown = true } else { withAnimation(.cavnarEase(1.2).delay(0.15)) { grown = true } }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Restaurant DNA shape: " + axes.map { "\($0.label) \($0.score.map(String.init) ?? "still learning")" }.joined(separator: ", "))
    }
}

// MARK: - The strand

/// Every dimension a rung of a slowly turning double helix: lit ember when
/// measured, dashed while still learning. Still under Reduce Motion.
struct DNAHelix: View {
    let lit: [Bool]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var start = Date()

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { tl in
            let t = reduceMotion ? 0 : tl.date.timeIntervalSince(start)
            Canvas { ctx, size in
                let n = lit.count
                guard n > 1 else { return }
                let top: CGFloat = 26, bot = size.height - 40
                let A = min(size.width * 0.26, 92)
                let grow = reduceMotion ? 1 : min(1, t / 1.4)
                let shown = Int((Double(n) * grow).rounded(.up))
                func geo(_ i: Int) -> (y: CGFloat, x1: CGFloat, x2: CGFloat, d: Double) {
                    let y = top + CGFloat(i) * (bot - top) / CGFloat(n - 1)
                    let ph = t * 0.55 + Double(i) * 0.34
                    return (y, size.width / 2 + A * CGFloat(sin(ph)), size.width / 2 - A * CGFloat(sin(ph)), cos(ph))
                }
                for i in 0..<shown {
                    let g = geo(i)
                    var rung = Path(); rung.move(to: CGPoint(x: g.x1, y: g.y)); rung.addLine(to: CGPoint(x: g.x2, y: g.y))
                    let a = 0.35 + 0.45 * (g.d * 0.5 + 0.5)
                    if lit[i] {
                        ctx.stroke(rung, with: .linearGradient(Gradient(colors: [Color.cavnarEmber.opacity(a), Color.cavnarEmber2.opacity(a)]),
                                                               startPoint: CGPoint(x: g.x1, y: 0), endPoint: CGPoint(x: g.x2, y: 0)),
                                   lineWidth: 1.6)
                    } else {
                        ctx.stroke(rung, with: .color(Color.cavnarInk3.opacity(0.18)), style: StrokeStyle(lineWidth: 1, dash: [3, 4]))
                    }
                }
                for s in 0..<2 {
                    var strand = Path()
                    for i in 0..<shown {
                        let g = geo(i), p = CGPoint(x: s == 0 ? g.x1 : g.x2, y: g.y)
                        if i == 0 { strand.move(to: p) } else { strand.addLine(to: p) }
                    }
                    ctx.stroke(strand, with: .color(Color.cavnarEmber2.opacity(0.42)), lineWidth: 1.3)
                }
                for i in 0..<shown {
                    let g = geo(i)
                    for s in 0..<2 {
                        let front = s == 0 ? g.d > 0 : g.d < 0
                        let r: CGFloat = front ? 3.2 : 2
                        let rect = CGRect(x: (s == 0 ? g.x1 : g.x2) - r, y: g.y - r, width: r * 2, height: r * 2)
                        if lit[i] {
                            var c2 = ctx
                            if front { c2.addFilter(.shadow(color: Color.cavnarEmber.opacity(0.9), radius: 6)) }
                            c2.fill(Path(ellipseIn: rect), with: .color(front ? Color.cavnarEmber2 : Color.cavnarEmber.opacity(0.7)))
                        } else {
                            ctx.stroke(Path(ellipseIn: rect), with: .color(Color.cavnarInk3.opacity(front ? 0.5 : 0.25)), lineWidth: 1)
                        }
                    }
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Every trait Cavnar AI measures, as a strand: \(lit.filter { $0 }.count) measured, \(lit.filter { !$0 }.count) still learning")
    }
}

// MARK: - What moves together

/// A thin ember link with light running along it, node to node.
struct DNAFlowLink: View {
    var vertical = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var phase: CGFloat = -0.5

    var body: some View {
        GeometryReader { g in
            let len = vertical ? g.size.height : g.size.width
            ZStack(alignment: vertical ? .top : .leading) {
                Rectangle().fill(Color.cavnarEmber.opacity(0.25))
                if !reduceMotion {
                    LinearGradient(colors: [.clear, .cavnarEmber2, .clear],
                                   startPoint: vertical ? .top : .leading, endPoint: vertical ? .bottom : .trailing)
                        .frame(width: vertical ? nil : len * 0.45, height: vertical ? len * 0.45 : nil)
                        .offset(x: vertical ? 0 : len * phase, y: vertical ? len * phase : 0)
                }
            }
            .clipped()
        }
        .frame(width: vertical ? 2 : nil, height: vertical ? nil : 2)
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.linear(duration: 2.2).repeatForever(autoreverses: false)) { phase = 1.05 }
        }
        .accessibilityHidden(true)
    }
}

// MARK: - Text with its figures set apart

enum DNAFigureText {
    /// `text` with every figure ("61%", "4.42★", "9.3 pts", "92") in
    /// `figureFont`/`figure`, the rest in `font`/`base`; "Watch:" in amber.
    static func make(_ text: String, font: Font, base: Color, figureFont: Font, figure: Color) -> Text {
        let pattern = "[+\\-]?\\d[\\d,]*(?:\\.\\d+)?(?:%|\u{2605}| pts|h\\b|\u{00D7})?"
        guard let re = try? NSRegularExpression(pattern: pattern) else { return Text(text).font(font).foregroundColor(base) }
        let ns = text as NSString
        var out = Text("")
        var at = 0
        func plain(_ s: String) -> Text {
            if let r = s.range(of: "Watch:") {
                return Text(String(s[..<r.lowerBound])).font(font).foregroundColor(base)
                    + Text("Watch:").font(font.weight(.bold)).foregroundColor(.cavnarAmber)
                    + Text(String(s[r.upperBound...])).font(font).foregroundColor(base)
            }
            return Text(s).font(font).foregroundColor(base)
        }
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            if m.range.location > at { out = out + plain(ns.substring(with: NSRange(location: at, length: m.range.location - at))) }
            out = out + Text(ns.substring(with: m.range)).font(figureFont).foregroundColor(figure)
            at = m.range.location + m.range.length
        }
        if at < ns.length { out = out + plain(ns.substring(from: at)) }
        return out
    }
}

struct DNACapLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            configuration.icon.font(.system(size: 7)).foregroundStyle(Color.cavnarEmber)
            configuration.title.cavnarText(.caption, color: .cavnarInk2)
        }
    }
}

/// An axis's 0-100 bar with the 50 tick; striped while still learning.
struct DNAGaugeBar: View {
    let score: Int?
    @State private var shown = false

    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(score == nil ? 0.04 : 0.07))
                if let score {
                    Capsule()
                        .fill(LinearGradient(colors: [.cavnarEmber, .cavnarEmber2], startPoint: .leading, endPoint: .trailing))
                        .frame(width: shown ? g.size.width * CGFloat(score) / 100 : 0)
                        .shadow(color: Color.cavnarEmber.opacity(0.45), radius: 5)
                }
                Rectangle().fill(Color.cavnarInk3.opacity(0.55)).frame(width: 1, height: 16).offset(x: g.size.width / 2)
            }
        }
        .frame(height: 8)
        .onAppear { withAnimation(.easeOut(duration: 1.2).delay(0.1)) { shown = true } }
    }
}

/// Rises and fades in the first time it comes on screen.
private struct DNAReveal: ViewModifier {
    let delay: Double
    @State private var shown = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(shown || reduceMotion ? 1 : 0)
            .offset(y: shown || reduceMotion ? 0 : 14)
            .onAppear {
                guard !shown else { return }
                withAnimation(.cavnarEase(0.7).delay(delay)) { shown = true }
            }
    }
}

extension View {
    func dnaReveal(delay: Double = 0) -> some View { modifier(DNAReveal(delay: delay)) }
}

// MARK: - A trait

struct DNATraitCard: View {
    let trait: RestaurantDNA.Story.Trait
    let dna: RestaurantDNA
    var featured = false
    @State private var open = false

    private var tint: Color { trait.isWatch ? .cavnarAmber : .cavnarEmber2 }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Image(systemName: DNAIcon.symbol(trait.icon))
                    .font(.system(size: 18, weight: .medium))
                    .foregroundStyle(tint)
                    .frame(width: 42, height: 42)
                    .background(RoundedRectangle(cornerRadius: 13, style: .continuous).fill(tint.opacity(0.14)))
                    .overlay(RoundedRectangle(cornerRadius: 13, style: .continuous).strokeBorder(tint.opacity(0.3), lineWidth: 1))
                Spacer()
                Text(trait.isWatch ? "Watch" : "Trait")
                    .cavnarText(.tag, color: trait.isWatch ? .cavnarAmber : .cavnarInk2)
            }
            Text(trait.name)
                .cavnarText(.headline)
                .padding(.top, 14)
            // One figure size on every trait card (iOS re-audit H9): figureM,
            // never 52/40pt per trait.
            Text(trait.figure)
                .cavnarText(.figureM)
                .minimumScaleFactor(0.85)
                .lineLimit(1)
                .padding(.top, 6)
            Text(trait.figureLabel)
                .cavnarText(.secondary)
                .padding(.top, 2)
            viz.padding(.top, 16)
            DNAFigureText.make(trait.sentence, font: .cavnar(.secondary), base: .cavnarInk2,
                               figureFont: .cavnarNumber(CavnarText.secondary.size, weight: 600,
                                                         relativeTo: CavnarText.secondary.textStyle),
                               figure: .cavnarInk)
                .lineSpacing(CavnarText.secondary.lineSpacing)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 14)
            if let st = trait.strength {
                HStack(spacing: 10) {
                    Text(trait.isWatch ? "How strongly" : "Strength").cavnarText(.caption, color: .cavnarInk2)
                    DNAMeter(value: st, tint: tint)
                    HomeMixedText.make("\(st)", role: .caption, color: .cavnarInk2)
                }
                .padding(.top, 14)
            }
            evidence.padding(.top, 12)
        }
        .padding(20)
        .background(
            ZStack(alignment: .topTrailing) {
                RoundedRectangle(cornerRadius: 24, style: .continuous)
                    .fill(LinearGradient(colors: [Color.white.opacity(0.055), Color.white.opacity(0.015)], startPoint: .top, endPoint: .bottom))
                Circle().fill(RadialGradient(colors: [tint.opacity(0.2), .clear], center: .center, startRadius: 0, endRadius: 120))
                    .frame(width: 240, height: 240).offset(x: 80, y: -100)
            }
            .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        )
        .overlay(RoundedRectangle(cornerRadius: 24, style: .continuous).strokeBorder(Color.cavnarInk3.opacity(featured ? 0.25 : 0.14), lineWidth: 1))
    }

    @ViewBuilder
    private var viz: some View {
        let v = dna.value
        switch trait.key {
        case "weekend_driven", "weekday_business":
            if let x = v("weekend_share") { DNASplitBar(weekendShare: x) }
        case "long_hours":
            if let x = v("open_hours") { ringRow(x / 168, "\(Int(x))h", "\(Int(x)) of the 168 hours in a week. The stated benchmark is about 70.") }
        case "overtime_controlled":
            if let x = v("overtime_intensity") { DNAGauge(lo: 0, hi: 0.1, value: x, typical: 0.03, format: Self.pct, label: "Overtime share of hours") }
        case "answers_reviews":
            if let x = v("reply_rate") { ringRow(x, Self.pct(x), "Of the last 30 days\u{2019} reviews. The stated benchmark is about 60%.") }
        case "open_to_advice":
            if let x = v("rec_uptake") { ringRow(x, Self.pct(x), "Of recommendations answered or expired in 90 days.") }
        case "well_rated", "guest_favorite":
            if let x = v("rating_level") { DNAStars(rating: x) }
        case "tight_register":
            VStack(spacing: 12) {
                if let c = v("comp_rate") { DNAGauge(lo: 0, hi: 3, value: c, typical: 1.5, format: Self.pt1, label: "Comps") }
                if let x = v("void_rate") { DNAGauge(lo: 0, hi: 2, value: x, typical: 1, format: Self.pt1, label: "Voids") }
            }
        case "uneven_labor_days":
            if let x = v("labor_swing") { DNAGauge(lo: 0, hi: 15, value: x, typical: 5, format: { String(format: "%.1f pts", $0) }, label: "Day-to-day labor % swing", watch: true) }
        case "labor_efficient":
            if let x = v("labor_pct") { DNAGauge(lo: 20, hi: 40, value: x, typical: 30, format: Self.pt1, label: "Labor % of sales") }
        case "staffs_to_demand":
            if let x = v("labor_flex") { DNAGauge(lo: 0, hi: 1.2, value: x, typical: 0.5, format: { String(format: "%.2f", $0) }, label: "Hours per 1% of sales") }
        case "steady_demand", "swingy_demand":
            if let x = v("sales_volatility") { DNAGauge(lo: 0, hi: 0.5, value: x, typical: 0.2, format: Self.pct, label: "Day-to-day swing", watch: trait.isWatch) }
        case "highly_predictable":
            if let x = v("demand_predictability") { ringRow(x, Self.pct(x), "Better than a naive guess, over scored weekly forecasts.") }
        default:
            EmptyView()
        }
    }

    private func ringRow(_ f: Double, _ label: String, _ note: String) -> some View {
        HStack(spacing: 14) {
            DNARing(fraction: f, size: 64, label: label)
            Text(note).cavnarText(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    private var evidence: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                Haptic.light()
                withAnimation(.cavnarEase()) { open.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Text("How it was measured").font(.cavnar(.label))
                    Image(systemName: "chevron.down").font(.cavnar(.caption)).rotationEffect(.degrees(open ? 180 : 0))
                        .accessibilityHidden(true)
                }
                .foregroundStyle(Color.cavnarEmber2)
                .frame(minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open {
                CavnarMixedText("Earned by: " + trait.rule, role: .secondary)
                ForEach(trait.dims, id: \.self) { key in
                    if let d = dna.dimensions.first(where: { $0.key == key }) {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(d.label).cavnarText(.secondary, color: .cavnarInk)
                                Spacer()
                                HomeMixedText.make(d.figure, role: .secondary, color: .cavnarInk)
                            }
                            if let b = d.basis { CavnarMixedText(b, role: .caption, color: .cavnarInk2) }
                            if let t = d.trendLine {
                                CavnarMixedText(t, role: .caption,
                                                color: d.trendTone == .cavnarInk3 ? .cavnarInk2 : d.trendTone)
                            }
                            if d.history.count >= 2 {
                                DNASparkline(points: d.history, tone: d.trendTone == .cavnarInk3 ? .cavnarEmber2 : d.trendTone).frame(height: 40)
                            }
                        }
                        .padding(.top, 10)
                    }
                }
            }
        }
        .overlay(alignment: .top) { Rectangle().fill(Color.cavnarInk3.opacity(0.12)).frame(height: 1) }
    }

    static func pct(_ x: Double) -> String { "\(Int((x * 100).rounded()))%" }
    static func pt1(_ x: Double) -> String { String(format: "%.1f%%", x) }
}

struct DNAMeter: View {
    let value: Int
    let tint: Color
    @State private var shown = false
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.07))
                Capsule().fill(tint).frame(width: shown ? g.size.width * CGFloat(value) / 100 : 0)
            }
        }
        .frame(height: 4)
        .onAppear { withAnimation(.easeOut(duration: 1.3).delay(0.2)) { shown = true } }
    }
}

// MARK: - Home: the DNA row, inside More

/// Restaurant DNA on Home (iOS re-audit H6, 10/8/26): one compact row in
/// More — it carries no decision, so it never outshouts the ones above it.
/// The full screen is unchanged. A profile still forming shows nothing on
/// Home (it was a permanent card for every new account); a /dna failure is
/// one line with a retry, never a silent gap (L16).
struct DNAHomeCard: View {
    let model: RestaurantDNAViewModel
    let onOpen: () -> Void

    var body: some View {
        Group {
            if let d = model.dna, let p = d.profile {
                if p.available, let s = d.story {
                    row(s)
                }
            } else if model.isLoading {
                CavnarSkeletonLines(widths: [0.6], lineHeight: 13, spacing: 8)
                    .padding(.vertical, CavnarSpace.xs)
            } else if model.errorMessage != nil {
                Button {
                    Haptic.light()
                    Task { await model.load() }
                } label: {
                    HStack(spacing: CavnarSpace.xs) {
                        Text("Restaurant DNA couldn\u{2019}t load").cavnarText(.secondary)
                        Text("Try again").cavnarText(.label, color: .cavnarEmber2)
                        Spacer(minLength: 0)
                    }
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
            }
        }
        .task { if model.dna == nil { await model.load() } }
    }

    private func row(_ s: RestaurantDNA.Story) -> some View {
        let o = s.observed
        return Button {
            Haptic.light()
            onOpen()
        } label: {
            HStack(alignment: .center, spacing: CavnarSpace.s) {
                DNARadar(axes: s.axes, mini: true)
                    .frame(width: 44, height: 44)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Restaurant DNA").cavnarText(.label)
                    CavnarMixedText("\(o?.measured ?? 0) of \(o?.of ?? 0) measured \u{00B7} \(s.traits.count) trait\(s.traits.count == 1 ? "" : "s") earned",
                                    role: .secondary)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk2)
                    .accessibilityHidden(true)
            }
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens your Restaurant DNA")
    }
}
