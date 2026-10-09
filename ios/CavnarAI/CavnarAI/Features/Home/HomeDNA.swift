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
        errorMessage = nil
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
                            learningSection(s)
                            // "Web explains. iPhone decides." (iOS re-audit
                            // M18): the axis rows, what moves together and
                            // every measurement are the web's /dna page.
                            CavnarWebLinkRow(title: "Every measurement and what moves together",
                                             subtitle: "Each axis, each dimension\u{2019}s weeks and how it was measured",
                                             path: "dna", actionLabel: "See it on the web")
                                .padding(.top, CavnarSpace.section)
                            if let basis = s.basis {
                                Text(basis + (p.note.map { " " + $0 } ?? ""))
                                    .cavnarText(.caption, color: .cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .padding(.top, CavnarSpace.xl)
                            }
                        } else {
                            Text("Your DNA is still forming.")
                                .cavnarText(.title)
                            Text(p.whyNot ?? "Your profile builds with the nightly pass \u{2014} check back tomorrow.")
                                .cavnarText(.body)
                                .padding(.top, 10)
                        }
                    } else if model.isLoading {
                        CavnarSkeletonLines(widths: [0.4, 0.95, 0.8, 0.6], lineHeight: 15, spacing: 14)
                            .padding(.top, 20)
                    } else {
                        Text(model.errorMessage ?? "Couldn\u{2019}t read your profile.")
                            .cavnarText(.body, color: .cavnarRedText)
                        Button {
                            Haptic.light()
                            Task { await model.load() }
                        } label: {
                            Text("Try again").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
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
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel("Close Restaurant DNA")
            Text("Restaurant DNA")
                .cavnarText(.headline)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
            Spacer()
            if let asOf = model.dna?.profile?.asOf {
                CavnarMixedText("as of " + asOf, role: .caption, color: .cavnarInk2)
            }
        }
        .padding(.horizontal, 10)
        .padding(.trailing, 10)
        .frame(minHeight: 54)
        .background(.ultraThinMaterial)
        .overlay(alignment: .bottom) { Rectangle().fill(Color.cavnarInk3.opacity(0.12)).frame(height: 1) }
    }

    private func sectionHead(_ k: String, _ title: String, _ sub: String?) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker(k)
            Text(title)
                .cavnarText(.headline)
                .fixedSize(horizontal: false, vertical: true)
            if let sub {
                Text(sub)
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, CavnarSpace.section)
    }

    // MARK: the read

    /// The read in one figure (iOS re-audit H9): the headline at the title
    /// role, ONE figureXL — how much of the profile is filled in — and the
    /// counts that were four 27pt tiles as one line under it.
    private func hero(_ p: RestaurantDNA.Profile, _ s: RestaurantDNA.Story, _ d: RestaurantDNA) -> some View {
        let parts = s.headParts
        let o = s.observed
        return VStack(alignment: .leading, spacing: 0) {
            CavnarKicker("Restaurant DNA" + (p.asOf.map { " \u{00B7} as of \($0)" } ?? ""))
            HomeMixedText.make(parts.head, role: .title, numberColor: .cavnarEmber2)
                .lineSpacing(CavnarText.title.lineSpacing)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 14)
                .dnaReveal()
            if !parts.rest.isEmpty {
                CavnarMixedText(parts.rest, role: .body, color: .cavnarInk2, numberColor: .cavnarInk)
                    .padding(.top, 14)
                    .dnaReveal(delay: 0.25)
            }
            if let lede = s.lede {
                CavnarMixedText(lede, role: .secondary)
                    .padding(.top, 12)
            }
            AccountFlowLayout(spacing: 8, lineSpacing: 8) {
                ForEach(s.identity) { DNAChip(label: $0.label) }
                ForEach(s.orderedTraits) { t in DNAChip(label: t.name, icon: t.icon, earned: true, watch: t.isWatch) }
            }
            .padding(.top, 18)
            .dnaReveal(delay: 0.35)
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                Text("\(o?.coveragePct ?? 0)%")
                    .cavnarText(.figureXL)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                    .contentTransition(.numericText())
                CavnarMixedText(Self.coverageLine(o), role: .secondary)
            }
            .padding(.top, 22)
            .accessibilityElement(children: .combine)
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
            // Flexible (L10): the strand keeps its room, the labels under
            // it grow with the text size instead of clipping.
            .frame(maxWidth: .infinity, minHeight: 300)
            .background(
                RoundedRectangle(cornerRadius: 26, style: .continuous)
                    .fill(RadialGradient(colors: [Color.cavnarEmber.opacity(0.14), Color.white.opacity(0.02)],
                                         center: .center, startRadius: 0, endRadius: 220))
            )
            .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).strokeBorder(Color.cavnarInk3.opacity(0.14), lineWidth: 1))
            .padding(.top, 22)
        }
    }

    /// "of the profile filled in · 9 of 24 traits measured · 15 still
    /// learning · 214 nights of sales watched since 3/1/26".
    static func coverageLine(_ o: RestaurantDNA.Story.Observed?) -> String {
        var bits = ["of the profile filled in",
                    "\(o?.measured ?? 0) of \(o?.of ?? 0) traits measured",
                    "\(o?.learning ?? 0) still learning"]
        if let n = o?.nights, n > 0 {
            bits.append("\(n) nights of sales watched" + (o?.since.map { " since \($0)" } ?? ""))
        }
        return bits.joined(separator: " \u{00B7} ")
    }

    // MARK: the shape

    @ViewBuilder
    private func shapeSection(_ s: RestaurantDNA.Story) -> some View {
        if s.axes.count >= 3 {
            VStack(alignment: .leading, spacing: 0) {
                sectionHead("The shape", "How it measures up",
                            "Each axis runs 0 to 100, and the dashed ring at 50 is Cavnar AI\u{2019}s stated benchmark. Dashed spokes are still learning.")
                DNARadar(axes: s.axes)
                    .padding(.horizontal, 22)
                    .padding(.top, 18)
            }
        }
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
                                .font(.cavnar(.body))
                                .foregroundStyle(Color.cavnarInk2)
                                .frame(width: 46, height: 46)
                                .overlay(Circle().strokeBorder(Color.cavnarInk3.opacity(0.35), style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
                        }
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 8) {
                                Text(l.name).cavnarText(.label)
                                if near {
                                    Text("Almost there").cavnarText(.tag, color: .cavnarEmber2)
                                }
                            }
                            CavnarMixedText(l.line, role: .secondary)
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
