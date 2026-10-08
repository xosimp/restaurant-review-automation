import SwiftUI
import Charts
import Observation

/// Restaurant DNA (parity audit #98; redesigned 10/8/26) — the restaurant's
/// OWN operational profile (intelligence/dna.py, GET /mobile/api/dna) and
/// its story (intelligence/dna_story.py): the read, earned traits, the
/// shape, what moves together, what is still learning, then every
/// measurement. A dimension below its minimum data is "—" with what would
/// measure it — never 0. Never another restaurant's figure, never a
/// z-score; a trait only from a measured figure against its stated rule.
/// The payload is projected by the login's module view.

struct RestaurantDNA: Decodable {
    struct Point: Decodable, Identifiable {
        let week: String
        let value: Double
        var id: String { week }
    }

    struct Trend: Decodable {
        let previousDisplay: String?
        let direction: String?
        enum CodingKeys: String, CodingKey {
            case direction
            case previousDisplay = "previous_display"
        }
    }

    struct Dimension: Decodable, Identifiable {
        let key: String
        let label: String
        let display: String?
        /// The raw figure (a share, a rating, hours) for a trait's own
        /// chart; nil for a categorical or unmeasured dimension.
        let value: Double?
        let unit: String?
        let better: String?
        let measured: Bool
        let basis: String?
        let dormant: Bool
        let needs: String?
        let trend: Trend?
        let history: [Point]
        var id: String { key }

        enum CodingKeys: String, CodingKey {
            case key, label, display, value, unit, better, measured, basis, dormant, needs, trend, history
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = try c.decode(String.self, forKey: .key)
            label = ((try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil) ?? key
            display = (try? c.decodeIfPresent(String.self, forKey: .display)) ?? nil
            value = (try? c.decodeIfPresent(Double.self, forKey: .value)) ?? nil
            unit = (try? c.decodeIfPresent(String.self, forKey: .unit)) ?? nil
            better = (try? c.decodeIfPresent(String.self, forKey: .better)) ?? nil
            measured = ((try? c.decodeIfPresent(Bool.self, forKey: .measured)) ?? nil) ?? false
            basis = (try? c.decodeIfPresent(String.self, forKey: .basis)) ?? nil
            dormant = ((try? c.decodeIfPresent(Bool.self, forKey: .dormant)) ?? nil) ?? false
            needs = (try? c.decodeIfPresent(String.self, forKey: .needs)) ?? nil
            trend = (try? c.decodeIfPresent(Trend.self, forKey: .trend)) ?? nil
            history = (try? c.decodeIfPresent(HomeLenientListDecodable<Point>.self, forKey: .history))?.items ?? []
        }

        /// The figure, or "—" below its floor — never 0.
        var figure: String { measured ? (display ?? "\u{2014}") : "\u{2014}" }

        /// "Up from 28% four weeks ago" / "Steady against four weeks ago".
        var trendLine: String? {
            guard measured, let t = trend, let dir = t.direction else { return nil }
            switch dir {
            case "steady": return "Steady against four weeks ago"
            case "up": return "Up from \(t.previousDisplay ?? "before") four weeks ago"
            case "down": return "Down from \(t.previousDisplay ?? "before") four weeks ago"
            default: return nil
            }
        }

        /// Green / red only when the dimension states which way is better;
        /// otherwise a move is a fact, in ink.
        var trendTone: Color {
            guard let dir = trend?.direction, dir != "steady", let better else { return .cavnarInk3 }
            let good = (dir == "up" && better == "higher") || (dir == "down" && better == "lower")
            return good ? .cavnarGreen : .cavnarRed
        }

        /// What it would take, for a dimension not measured yet.
        var needsLine: String? {
            guard !measured else { return nil }
            if dormant { return "Not measurable on Cavnar AI yet \u{2014} needs " + (needs ?? "a source") }
            return needs.map { "Needs " + $0 }
        }
    }

    struct Family: Decodable, Identifiable {
        let key: String
        let label: String
        let dimensions: [Dimension]
        var id: String { key }
        enum CodingKeys: String, CodingKey { case key, label, dimensions }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = try c.decode(String.self, forKey: .key)
            label = ((try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil) ?? key
            dimensions = (try? c.decodeIfPresent(HomeLenientListDecodable<Dimension>.self, forKey: .dimensions))?.items ?? []
        }
    }

    struct Profile: Decodable {
        let available: Bool
        let whyNot: String?
        let asOf: String?
        let measured: Int?
        let of: Int?
        let coveragePct: Int?
        let families: [Family]
        let note: String?
        enum CodingKeys: String, CodingKey {
            case available, measured, of, families, note
            case whyNot = "why_not"
            case asOf = "as_of"
            case coveragePct = "coverage_pct"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            available = ((try? c.decodeIfPresent(Bool.self, forKey: .available)) ?? nil) ?? false
            whyNot = (try? c.decodeIfPresent(String.self, forKey: .whyNot)) ?? nil
            asOf = (try? c.decodeIfPresent(String.self, forKey: .asOf)) ?? nil
            measured = (try? c.decodeIfPresent(Int.self, forKey: .measured)) ?? nil
            of = (try? c.decodeIfPresent(Int.self, forKey: .of)) ?? nil
            coveragePct = (try? c.decodeIfPresent(Int.self, forKey: .coveragePct)) ?? nil
            families = (try? c.decodeIfPresent(HomeLenientListDecodable<Family>.self, forKey: .families))?.items ?? []
            note = (try? c.decodeIfPresent(String.self, forKey: .note)) ?? nil
        }

        /// "9 of 24 measured · 38%".
        var coverageLine: String? {
            guard let m = measured, let t = of, t > 0 else { return nil }
            return "\(m) of \(t) measured" + (coveragePct.map { " \u{00B7} \($0)%" } ?? "")
        }
    }

    let ok: Bool
    let profile: Profile?
    /// The told DNA (HomeDNAStory.swift); nil from a server before 10/8/26.
    let story: Story?
    let error: String?

    /// Every dimension, in family order, for a trait's chart and the strand.
    var dimensions: [Dimension] { (profile?.families ?? []).flatMap(\.dimensions) }
    func value(_ key: String) -> Double? { dimensions.first { $0.key == key && $0.measured }?.value }

    /// The Monday of an ISO week ("2026-W38") as M/D/YY, for the chart's
    /// ends — an owner never reads a week code.
    static func weekStart(_ week: String) -> String? {
        let parts = week.split(separator: "-")
        guard parts.count == 2, let y = Int(parts[0]), parts[1].hasPrefix("W"),
              let w = Int(parts[1].dropFirst()) else { return nil }
        var cal = Calendar(identifier: .iso8601)
        cal.timeZone = TimeZone(secondsFromGMT: 0)!
        guard let d = cal.date(from: DateComponents(weekday: 2, weekOfYear: w, yearForWeekOfYear: y)) else { return nil }
        return CavnarDate.mdy(d, in: TimeZone(secondsFromGMT: 0)!)
    }
}

@Observable
@MainActor
final class RestaurantDNAViewModel {
    private(set) var dna: RestaurantDNA?
    private(set) var isLoading = true
    var errorMessage: String?
    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let r: RestaurantDNA = try await client.send("/mobile/api/dna", hapticOnError: false)
            dna = r
            if !r.ok { errorMessage = r.error ?? "Couldn\u{2019}t read your profile." }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t reach Cavnar AI."
        }
    }
}

/// The DNA screen, full-screen from Home's DNA card (owner, 10/8/26 — the
/// web's /dna page): the read, the strand, the shape, the traits, what
/// moves together, what is still learning, then every measurement.
struct RestaurantDNAScreen: View {
    let model: RestaurantDNAViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack(alignment: .top) {
            Color.cavnarPaper.ignoresSafeArea()
            RadialGradient(colors: [Color.cavnarEmber.opacity(0.22), .clear], center: .topTrailing,
                           startRadius: 0, endRadius: 520)
                .ignoresSafeArea()
                .allowsHitTesting(false)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if let d = model.dna, let p = d.profile {
                        if p.available, let s = d.story {
                            hero(p, s, d)
                            shapeSection(s).dnaReveal()
                            traitsSection(s, d)
                            chainsSection(s)
                            learningSection(s)
                            evidenceSection(p)
                            if let basis = s.basis {
                                Text(basis + (p.note.map { " " + $0 } ?? ""))
                                    .font(.cavnarBody(12, weight: 500))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .padding(.top, 44)
                            }
                        } else {
                            Text("Your DNA is still forming.")
                                .font(.cavnarHeadline(30, weight: .medium))
                                .foregroundStyle(Color.cavnarInk)
                            Text(p.whyNot ?? "Your profile builds with the nightly pass \u{2014} check back tomorrow.")
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk2)
                                .padding(.top, 10)
                        }
                    } else if model.isLoading {
                        CavnarSkeletonLines(widths: [0.4, 0.95, 0.8, 0.6], lineHeight: 15, spacing: 14)
                            .padding(.top, 20)
                    } else {
                        Text(model.errorMessage ?? "Couldn\u{2019}t read your profile.")
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarRed)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.top, 70)
                .padding(.bottom, 80)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .cavnarEmberRefreshable { await model.load() }
            topBar
        }
        .task { if model.dna == nil { await model.load() } }
    }

    private var topBar: some View {
        HStack(spacing: 12) {
            Button {
                Haptic.light()
                dismiss()
            } label: {
                Image(systemName: "chevron.down")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.cavnarInk)
                    .frame(width: 44, height: 44)
            }
            .accessibilityLabel("Close Restaurant DNA")
            Text("Restaurant DNA")
                .font(.cavnarHeadline(17, weight: .medium))
                .foregroundStyle(Color.cavnarInk)
            Spacer()
            if let asOf = model.dna?.profile?.asOf {
                HomeMixedText.make("as of " + asOf, size: 12.5, weight: 500, color: .cavnarInk3)
            }
        }
        .padding(.horizontal, 10)
        .padding(.trailing, 10)
        .frame(height: 54)
        .background(.ultraThinMaterial)
        .overlay(alignment: .bottom) { Rectangle().fill(Color.cavnarInk3.opacity(0.12)).frame(height: 1) }
    }

    private func kicker(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(CavnarType.kicker, weight: 700))
            .tracking(1.8)
            .foregroundStyle(Color.cavnarEmber)
    }

    private func sectionHead(_ k: String, _ title: String, _ sub: String?) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            kicker(k)
            Text(title)
                .font(.cavnarHeadline(27, weight: .medium))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            if let sub {
                Text(sub)
                    .font(.cavnarBody(14.5))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, 64)
    }

    // MARK: the read

    private func hero(_ p: RestaurantDNA.Profile, _ s: RestaurantDNA.Story, _ d: RestaurantDNA) -> some View {
        let parts = s.headParts
        let o = s.observed
        return VStack(alignment: .leading, spacing: 0) {
            kicker("Restaurant DNA" + (p.asOf.map { " \u{00B7} as of \($0)" } ?? ""))
            DNAFigureText.make(parts.head, font: .cavnarHeadline(31, weight: .medium), base: .cavnarInk,
                               figureFont: .cavnarHeadline(31, weight: .medium), figure: .cavnarEmber2)
                .lineSpacing(2)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 14)
                .dnaReveal()
            if !parts.rest.isEmpty {
                DNAFigureText.make(parts.rest, font: .cavnarBody(16.5), base: .cavnarInk2,
                                   figureFont: .cavnarNumber(16.5, weight: 600), figure: .cavnarInk)
                    .lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 14)
                    .dnaReveal(delay: 0.25)
            }
            if let lede = s.lede {
                HomeMixedText.make(lede, size: 13, weight: 500, color: .cavnarInk3)
                    .padding(.top, 12)
            }
            AccountFlowLayout(spacing: 8, lineSpacing: 8) {
                ForEach(s.identity) { DNAChip(label: $0.label) }
                ForEach(s.orderedTraits) { t in DNAChip(label: t.name, icon: t.icon, earned: true, watch: t.isWatch) }
            }
            .padding(.top, 18)
            .dnaReveal(delay: 0.35)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                if let n = o?.nights, n > 0 { stat("\(n)", "nights of sales watched" + (o?.since.map { " since \($0)" } ?? "")) }
                stat("\(o?.measured ?? 0)", "of \(o?.of ?? 0) traits measured")
                stat("\(o?.learning ?? 0)", "still learning")
                stat("\(o?.coveragePct ?? 0)%", "of the profile filled in")
            }
            .padding(.top, 22)
            .dnaReveal(delay: 0.4)
            ZStack(alignment: .bottom) {
                DNAHelix(lit: d.dimensions.map(\.measured))
                HStack {
                    Label("\(d.dimensions.filter(\.measured).count) measured", systemImage: "circle.fill")
                    Spacer()
                    Label("\(d.dimensions.filter { !$0.measured }.count) still learning", systemImage: "circle.dashed")
                }
                .labelStyle(DNACapLabel())
                .padding(.horizontal, 16)
                .padding(.bottom, 12)
            }
            .frame(height: 330)
            .background(
                RoundedRectangle(cornerRadius: 26, style: .continuous)
                    .fill(RadialGradient(colors: [Color.cavnarEmber.opacity(0.14), Color.white.opacity(0.02)],
                                         center: .center, startRadius: 0, endRadius: 220))
            )
            .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).strokeBorder(Color.cavnarInk3.opacity(0.14), lineWidth: 1))
            .padding(.top, 22)
        }
    }

    private func stat(_ big: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(big)
                .font(.cavnarNumber(27, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .contentTransition(.numericText())
            Text(label)
                .font(.cavnarBody(12, weight: 500))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Color.white.opacity(0.035)))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Color.cavnarInk3.opacity(0.12), lineWidth: 1))
    }

    // MARK: the shape

    @ViewBuilder
    private func shapeSection(_ s: RestaurantDNA.Story) -> some View {
        if s.axes.count >= 3 {
            VStack(alignment: .leading, spacing: 0) {
                sectionHead("The shape", "How it measures up",
                            "Each axis runs 0 to 100, and the dashed ring at 50 is a typical restaurant on Cavnar AI\u{2019}s stated benchmarks. Dashed spokes are still learning.")
                DNARadar(axes: s.axes)
                    .padding(.horizontal, 22)
                    .padding(.top, 18)
                VStack(spacing: 0) {
                    ForEach(s.axes) { a in
                        axisRow(a)
                        if a.id != s.axes.last?.id { Rectangle().fill(Color.cavnarInk3.opacity(0.1)).frame(height: 1) }
                    }
                }
                .padding(.top, 10)
            }
        }
    }

    private func axisRow(_ a: RestaurantDNA.Story.Axis) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 1) {
                Text(a.label)
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(a.score == nil ? Color.cavnarInk3 : Color.cavnarInk)
                Text(a.score == nil ? "still learning" : "\(a.measured) of \(a.of) measured")
                    .font(.cavnarBody(11, weight: 500))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .frame(width: 128, alignment: .leading)
            DNAGaugeBar(score: a.score)
            Text(a.score.map(String.init) ?? "\u{2014}")
                .font(.cavnarNumber(15, weight: 600))
                .foregroundStyle(a.score == nil ? Color.cavnarInk3 : Color.cavnarInk)
                .frame(width: 34, alignment: .trailing)
        }
        .padding(.vertical, 10)
        .accessibilityElement(children: .combine)
    }

    // MARK: traits

    @ViewBuilder
    private func traitsSection(_ s: RestaurantDNA.Story, _ d: RestaurantDNA) -> some View {
        sectionHead("Traits", s.traits.isEmpty ? "No traits earned yet" : "What it\u{2019}s become",
                    s.traits.isEmpty ? "A trait is named only when a measured figure clears its rule. As more of your data comes in, they appear here."
                                     : "Each trait is earned by a measured figure against the rule shown under it.")
        ForEach(Array(s.orderedTraits.enumerated()), id: \.element.id) { i, t in
            DNATraitCard(trait: t, dna: d, featured: i == 0 && !t.isWatch)
                .padding(.top, 14)
                .dnaReveal()
        }
    }

    // MARK: what moves together

    @ViewBuilder
    private func chainsSection(_ s: RestaurantDNA.Story) -> some View {
        if !s.connections.isEmpty {
            sectionHead("How it connects", "What moves together",
                        "Measured figures read side by side. Where one drives another, the measurement says how much.")
            ForEach(s.connections) { c in
                VStack(alignment: .leading, spacing: 12) {
                    Text(c.title.uppercased())
                        .font(.cavnarBody(11.5, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarInk3)
                    VStack(spacing: 0) {
                        ForEach(Array(c.nodes.enumerated()), id: \.offset) { i, n in
                            if i > 0 { DNAFlowLink(vertical: true).frame(height: 22) }
                            HStack(alignment: .firstTextBaseline) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(n.label).font(.cavnarBody(13.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                                    if let note = n.note { Text(note).font(.cavnarBody(11.5, weight: 500)).foregroundStyle(Color.cavnarInk3) }
                                }
                                Spacer()
                                Text(n.figure ?? "measuring")
                                    .font(.cavnarNumber(n.figure == nil ? 16 : 24, weight: 600))
                                    .foregroundStyle(n.figure == nil ? Color.cavnarInk3 : Color.cavnarInk)
                            }
                            .padding(14)
                            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Color.cavnarSurface))
                            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Color.cavnarInk3.opacity(0.16), lineWidth: 1))
                        }
                    }
                    DNAFigureText.make(c.sentence, font: .cavnarBody(15.5), base: .cavnarInk,
                                       figureFont: .cavnarNumber(15.5, weight: 600), figure: .cavnarInk)
                        .lineSpacing(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(18)
                .background(RoundedRectangle(cornerRadius: 22, style: .continuous).fill(Color.white.opacity(0.035)))
                .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).strokeBorder(Color.cavnarInk3.opacity(0.12), lineWidth: 1))
                .padding(.top, 14)
                .dnaReveal()
            }
        }
    }

    // MARK: still learning

    @ViewBuilder
    private func learningSection(_ s: RestaurantDNA.Story) -> some View {
        if !s.learning.isEmpty {
            sectionHead("Still learning", "\(s.learning.count) traits are still taking shape",
                        "Nothing here is guessed. Each fills in once its data clears the floor, closest first.")
            VStack(spacing: 10) {
                ForEach(s.learning) { l in
                    let near = (l.progress?.pct ?? 0) >= 75
                    HStack(spacing: 14) {
                        if let pr = l.progress {
                            DNARing(fraction: Double(pr.pct) / 100, size: 46, label: "\(pr.pct)%")
                        } else {
                            Image(systemName: DNAIcon.symbol(l.icon))
                                .font(.system(size: 17))
                                .foregroundStyle(Color.cavnarInk3)
                                .frame(width: 46, height: 46)
                                .overlay(Circle().strokeBorder(Color.cavnarInk3.opacity(0.35), style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
                        }
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 8) {
                                Text(l.name).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                                if near {
                                    Text("ALMOST THERE").font(.cavnarBody(10, weight: 700)).tracking(1).foregroundStyle(Color.cavnarEmber2)
                                }
                            }
                            HomeMixedText.make(l.line, size: 12.5, weight: 500, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(14)
                    .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(near ? Color.cavnarEmber.opacity(0.06) : Color.white.opacity(0.02)))
                    .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .strokeBorder(near ? Color.cavnarEmber.opacity(0.4) : Color.cavnarInk3.opacity(0.25),
                                      style: StrokeStyle(lineWidth: 1, dash: near ? [] : [4, 4])))
                    .accessibilityElement(children: .combine)
                }
            }
            .padding(.top, 16)
            .dnaReveal()
        }
    }

    // MARK: every measurement

    @ViewBuilder
    private func evidenceSection(_ p: RestaurantDNA.Profile) -> some View {
        sectionHead("The evidence", "Every measurement",
                    "All \(p.of ?? 0) dimensions, each with the figure, how it was measured and its weeks so far, or what would measure it.")
        VStack(spacing: 10) {
            ForEach(p.families) { family in
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(family.dimensions.enumerated()), id: \.element.id) { index, dim in
                            dimensionRow(dim)
                            if index < family.dimensions.count - 1 { AccountRowDivider() }
                        }
                    }
                    .padding(.top, 6)
                } label: {
                    HStack(spacing: 10) {
                        Text(family.label).font(.cavnarBody(15.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Text("\(family.dimensions.filter(\.measured).count) of \(family.dimensions.count)")
                            .font(.cavnarNumber(12.5, weight: 500))
                            .foregroundStyle(Color.cavnarInk3)
                        Spacer(minLength: 6)
                        HStack(spacing: 3) {
                            ForEach(family.dimensions) { dim in
                                Circle()
                                    .fill(dim.measured ? Color.cavnarEmber : .clear)
                                    .overlay(Circle().strokeBorder(dim.measured ? .clear : Color.cavnarInk3.opacity(0.5), style: StrokeStyle(lineWidth: 1, dash: [1.5, 1.5])))
                                    .frame(width: 6, height: 6)
                            }
                        }
                        .accessibilityHidden(true)
                    }
                }
                .tint(Color.cavnarInk3)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color.white.opacity(0.025)))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Color.cavnarInk3.opacity(0.12), lineWidth: 1))
            }
        }
        .padding(.top, 16)
    }

    private func dimensionRow(_ dim: RestaurantDNA.Dimension) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(dim.label)
                    .font(.cavnarBody(14.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Text(dim.figure)
                    .font(.cavnarNumber(17, weight: 700))
                    .foregroundStyle(dim.measured ? Color.cavnarInk : Color.cavnarInk3)
            }
            if dim.history.count >= 2 {
                DNASparkline(points: dim.history, tone: dim.trendTone == .cavnarInk3 ? .cavnarEmber2 : dim.trendTone)
                    .frame(height: 54)
                    .accessibilityLabel("\(dim.label) over \(dim.history.count) weeks")
                if let first = dim.history.first.flatMap({ RestaurantDNA.weekStart($0.week) }),
                   let last = dim.history.last.flatMap({ RestaurantDNA.weekStart($0.week) }) {
                    HStack {
                        HomeMixedText.make(first, size: 11, weight: 500, color: .cavnarInk3)
                        Spacer()
                        HomeMixedText.make(last, size: 11, weight: 500, color: .cavnarInk3)
                    }
                }
            }
            if let trend = dim.trendLine {
                HomeMixedText.make(trend, size: 12.5, weight: 600, color: dim.trendTone)
            }
            if let needs = dim.needsLine {
                HomeMixedText.make(needs, size: 12.5, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let basis = dim.basis, !basis.isEmpty {
                HomeMixedText.make(basis, size: 12, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 11)
        .accessibilityElement(children: .combine)
    }
}

/// One dimension's weeks as a small ember line with a soft fill — Swift
/// Charts, no axes (the ends are labelled under it with real dates).
struct DNASparkline: View {
    let points: [RestaurantDNA.Point]
    let tone: Color

    var body: some View {
        let values = points.map(\.value)
        let lo = values.min() ?? 0, hi = values.max() ?? 1
        let pad = max((hi - lo) * 0.2, abs(hi) * 0.02, 0.0001)
        Chart(Array(points.enumerated()), id: \.offset) { i, p in
            AreaMark(x: .value("Week", i), yStart: .value("Floor", lo - pad), yEnd: .value("Value", p.value))
                .foregroundStyle(LinearGradient(colors: [tone.opacity(0.28), tone.opacity(0)],
                                                startPoint: .top, endPoint: .bottom))
                .interpolationMethod(.monotone)
            LineMark(x: .value("Week", i), y: .value("Value", p.value))
                .foregroundStyle(tone)
                .lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round))
                .interpolationMethod(.monotone)
            if i == points.count - 1 {
                PointMark(x: .value("Week", i), y: .value("Value", p.value))
                    .foregroundStyle(tone)
                    .symbolSize(28)
            }
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartYScale(domain: (lo - pad)...(hi + pad))
        .chartLegend(.hidden)
    }
}
