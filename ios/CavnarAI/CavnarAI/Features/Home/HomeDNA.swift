import SwiftUI
import Charts
import Observation

/// Restaurant DNA (parity audit #98) — the restaurant's OWN operational
/// profile (intelligence/dna.py, GET /mobile/api/dna): how steady its sales
/// are, how labor follows them, how regularly waste is logged, how often
/// advice is acted on. Each dimension is a measured figure with the weeks
/// behind it, the move against four weeks ago and how it was measured. A
/// dimension below its minimum data is "—" with what would measure it —
/// never 0. Never another restaurant's figure, never a z-score, never a
/// personality label; the payload is projected by the login's module view.

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
            case key, label, display, unit, better, measured, basis, dormant, needs, trend, history
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            key = try c.decode(String.self, forKey: .key)
            label = ((try? c.decodeIfPresent(String.self, forKey: .label)) ?? nil) ?? key
            display = (try? c.decodeIfPresent(String.self, forKey: .display)) ?? nil
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
    let error: String?

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

/// The sheet, opened from Home's Results.
struct RestaurantDNASheet: View {
    @State private var viewModel = RestaurantDNAViewModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if let p = viewModel.dna?.profile {
                        if p.available {
                            header(p)
                            ForEach(p.families) { family in
                                familySection(family)
                            }
                            if let note = p.note {
                                CavnarCaveat(title: "Your own figures", detail: note)
                            }
                        } else {
                            Text(p.whyNot ?? "Your profile builds with the nightly pass \u{2014} check back tomorrow.")
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    } else if viewModel.isLoading {
                        CavnarSkeletonLines(widths: [0.5, 0.9, 0.7, 0.85], lineHeight: 13, spacing: 12)
                    } else {
                        Text(viewModel.errorMessage ?? "Couldn\u{2019}t read your profile.")
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarRed)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .cavnarEmberRefreshable { await viewModel.load() }
            .cavnarModuleBackground()
            .accountSheetChrome("Restaurant DNA")
        }
        .task { await viewModel.load() }
    }

    private func header(_ p: RestaurantDNA.Profile) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("YOUR OPERATING PROFILE")
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.6)
                .foregroundStyle(Color.cavnarEmber2)
            Text("How this restaurant runs, measured from its own data")
                .font(.cavnarHeadline(21))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            let bits = [p.coverageLine, p.asOf.map { "as of \($0)" }].compactMap { $0 }
            if !bits.isEmpty {
                HomeMixedText.make(bits.joined(separator: " \u{00B7} "), size: 13, weight: 600, color: .cavnarInk3)
            }
        }
    }

    private func familySection(_ family: RestaurantDNA.Family) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(family.label.uppercased())
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(family.dimensions.enumerated()), id: \.element.id) { index, dim in
                    dimensionRow(dim)
                    if index < family.dimensions.count - 1 { AccountRowDivider() }
                }
            }
            .cavnarCard()
        }
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
