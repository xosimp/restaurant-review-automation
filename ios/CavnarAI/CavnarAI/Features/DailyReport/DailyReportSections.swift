import SwiftUI

// The sections both nightly reports share (9/25/26): the key numbers with
// direction (dsr/kpis.py), the manager's Today's shift and Operations,
// AI insights, Tomorrow (dsr/tomorrow.py — prep, forecast, AI confidence)
// and "How did yesterday turn out?" (dsr/predictions.py). Every figure is
// the server's; a missing one is left out, never shown as zero.

struct DSRKPI: Decodable, Hashable, Identifiable {
    struct Change: Decodable, Hashable { let text: String; let tone: String? }
    struct Streak: Decodable, Hashable { let text: String; let tone: String? }
    struct Target: Decodable, Hashable {
        let label: String
        let valueText: String
        enum CodingKeys: String, CodingKey { case label; case valueText = "value_text" }
    }
    struct Peers: Decodable, Hashable {
        let available: Bool
        let label: String
        let valueText: String?
        let whyNot: String?
        let strengthPct: Int?
        enum CodingKeys: String, CodingKey {
            case available, label
            case valueText = "value_text"
            case whyNot = "why_not"
            case strengthPct = "strength_pct"
        }
    }

    let key: String
    let label: String
    let valueText: String
    let change: Change?
    let streak: Streak?
    let spark: [Double]
    let target: Target?
    let peers: Peers?
    let estimate: Bool
    let detail: String?
    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, label, change, streak, spark, target, peers, estimate, detail
        case valueText = "value_text"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        label = try c.decode(String.self, forKey: .label)
        valueText = (try? c.decode(String.self, forKey: .valueText)) ?? "—"
        change = try? c.decodeIfPresent(Change.self, forKey: .change)
        streak = try? c.decodeIfPresent(Streak.self, forKey: .streak)
        spark = (try? c.decodeIfPresent([Double].self, forKey: .spark)) ?? []
        target = try? c.decodeIfPresent(Target.self, forKey: .target)
        peers = try? c.decodeIfPresent(Peers.self, forKey: .peers)
        estimate = (try? c.decodeIfPresent(Bool.self, forKey: .estimate)) ?? false
        detail = try? c.decodeIfPresent(String.self, forKey: .detail)
    }
}

struct DSRShift: Decodable, Hashable {
    struct Row: Decodable, Hashable, Identifiable {
        let key: String
        let label: String
        let valueText: String
        let tone: String?
        var id: String { key }
        enum CodingKeys: String, CodingKey { case key, label, tone; case valueText = "value_text" }
    }
    let rows: [Row]
    let note: String?
    /// The manager's one-line verdict ("Labor 27.5%, on target · 1
    /// no-show"), server-built; nil on an older server.
    var verdict: String? = nil
}

struct DSRInsight: Decodable, Hashable, Identifiable {
    let kind: String
    let label: String
    let text: String
    var id: String { kind + text }
}

struct DSRTomorrow: Decodable, Hashable {
    /// "The day after · Friday · 9/26/26" — the weekday AND the M/D/YY date,
    /// as the web heads it (D3-13). Not "Tomorrow": the report is read the
    /// morning after the night it covers (owner, 9/29/26).
    var heading: String {
        ["The day after", weekday, date.flatMap { DSRFormat.isISODate($0) ? CavnarDate.mdy($0) : nil }]
            .compactMap { $0 }.joined(separator: " \u{00B7} ")
    }

    struct Item: Decodable, Hashable, Identifiable {
        let kind: String?
        let tone: String?
        let text: String
        var id: String { (kind ?? "") + text }
    }
    struct Forecast: Decodable, Hashable { let text: String; let basis: String? }
    /// The forecast's AI confidence. `pct` is null when too little of what
    /// the forecast uses was measured — the server then sends `label` "—";
    /// the based-on and missing lines still stand (DB, 9/25/26). `watch`:
    /// what to keep an eye on. Every field lenient, so one odd field never
    /// drops the Tomorrow card.
    struct Confidence: Decodable, Hashable {
        let pct: Int?
        let label: String?
        let basedOn: [String]
        let missing: [String]
        let watch: [String]
        let track: String?
        enum CodingKeys: String, CodingKey { case pct, label, missing, watch, track; case basedOn = "based_on" }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            if let i = try? c.decodeIfPresent(Int.self, forKey: .pct) { pct = i }
            else if let d = try? c.decodeIfPresent(Double.self, forKey: .pct) { pct = Int(d.rounded()) }
            else { pct = nil }
            label = try? c.decodeIfPresent(String.self, forKey: .label)
            basedOn = (try? c.decodeIfPresent([String].self, forKey: .basedOn)) ?? []
            missing = (try? c.decodeIfPresent([String].self, forKey: .missing)) ?? []
            watch = (try? c.decodeIfPresent([String].self, forKey: .watch)) ?? []
            track = try? c.decodeIfPresent(String.self, forKey: .track)
        }

        /// "70%", or the server's "—" when there is no figure — never 0%.
        var figure: String { pct.map { "\($0)%" } ?? (label?.isEmpty == false ? label! : "\u{2014}") }
    }
    /// The day after's labor: the published schedule priced at each
    /// person's pay over Cavnar AI's forecast (dsr.tomorrow.labor_plan) —
    /// the salaried share is the owner's (access.tomorrow_for strips it
    /// otherwise). Absent for a login without the Labor view.
    struct LaborPlan: Decodable, Hashable {
        let hours: Double?
        let hourlyCost: Double?
        let overtimeHours: Double?
        let hourlyPct: Double?
        let forecastNet: Double?
        let salariedTotalCost: Double?
        let salariedTotalPct: Double?
        let targetPct: Double?
        let targetSource: String?
        let basis: String?

        enum CodingKeys: String, CodingKey {
            case hours, basis
            case hourlyCost = "hourly_cost"
            case overtimeHours = "overtime_hours"
            case hourlyPct = "hourly_pct"
            case forecastNet = "forecast_net"
            case salariedTotalCost = "salaried_total_cost"
            case salariedTotalPct = "salaried_total_pct"
            case targetPct = "target_pct"
            case targetSource = "target_source"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            func num(_ k: CodingKeys) -> Double? { (try? c.decodeIfPresent(Double.self, forKey: k)) ?? nil }
            hours = num(.hours); hourlyCost = num(.hourlyCost); overtimeHours = num(.overtimeHours)
            hourlyPct = num(.hourlyPct); forecastNet = num(.forecastNet)
            salariedTotalCost = num(.salariedTotalCost); salariedTotalPct = num(.salariedTotalPct)
            targetPct = num(.targetPct)
            targetSource = (try? c.decodeIfPresent(String.self, forKey: .targetSource)) ?? nil
            basis = (try? c.decodeIfPresent(String.self, forKey: .basis)) ?? nil
        }

        /// Points over (+) or under (−) the target, to one decimal.
        func gap(pct: Double?) -> Double? {
            guard let pct, let t = targetPct else { return nil }
            return ((pct - t) * 10).rounded() / 10
        }

        /// "1.4 pts over the 28% target" / "On Cavnar AI's starting 30%" —
        /// a target the owner never set is never called theirs.
        func targetLine(pct: Double?) -> String? {
            guard let g = gap(pct: pct), let t = targetPct else { return nil }
            let soft = targetSource == "default"
            let tg = DSRFormat.pct(t)
            if g == 0 { return soft ? "On Cavnar AI\u{2019}s starting \(tg)" : "On the \(tg) target" }
            let pts = String(format: "%.1f", abs(g))
            return "\(pts) pts \(g > 0 ? "over" : "under") " + (soft ? "Cavnar AI\u{2019}s starting \(tg)" : "the \(tg) target")
        }
    }

    /// The payroll week's overtime if the schedule holds
    /// (dsr.tomorrow.overtime_outlook): who goes past 40 hours, the extra
    /// it costs, and a same-role teammate with room.
    struct Overtime: Decodable, Hashable {
        struct Person: Decodable, Hashable, Identifiable {
            let employee: String
            let role: String?
            let projectedHours: Double?
            let overtimeHours: Double?
            let extraCost: Double?
            let room: [String]
            var id: String { employee + (role ?? "") }
            enum CodingKeys: String, CodingKey {
                case employee, role, room
                case projectedHours = "projected_hours"
                case overtimeHours = "overtime_hours"
                case extraCost = "extra_cost"
            }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                employee = (try? c.decode(String.self, forKey: .employee)) ?? "Someone"
                role = (try? c.decodeIfPresent(String.self, forKey: .role)) ?? nil
                projectedHours = (try? c.decodeIfPresent(Double.self, forKey: .projectedHours)) ?? nil
                overtimeHours = (try? c.decodeIfPresent(Double.self, forKey: .overtimeHours)) ?? nil
                extraCost = (try? c.decodeIfPresent(Double.self, forKey: .extraCost)) ?? nil
                room = (try? c.decodeIfPresent([String].self, forKey: .room)) ?? []
            }
        }
        let people: [Person]
        let overCount: Int?
        let extraCost: Double?
        let basis: String?
        enum CodingKeys: String, CodingKey {
            case people, basis
            case overCount = "over_count"
            case extraCost = "extra_cost"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            people = (try? c.decodeIfPresent([Person].self, forKey: .people)) ?? []
            overCount = (try? c.decodeIfPresent(Int.self, forKey: .overCount)) ?? nil
            extraCost = (try? c.decodeIfPresent(Double.self, forKey: .extraCost)) ?? nil
            basis = (try? c.decodeIfPresent(String.self, forKey: .basis)) ?? nil
        }
    }

    let date: String?
    let weekday: String?
    let items: [Item]
    let scheduled: Int?
    let forecast: Forecast?
    let confidence: Confidence?
    var labor: LaborPlan? = nil
    var overtime: Overtime? = nil

    enum CodingKeys: String, CodingKey { case date, weekday, items, scheduled, forecast, confidence, labor, overtime }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = try? c.decodeIfPresent(String.self, forKey: .date)
        weekday = try? c.decodeIfPresent(String.self, forKey: .weekday)
        items = (try? c.decodeIfPresent([Item].self, forKey: .items)) ?? []
        scheduled = try? c.decodeIfPresent(Int.self, forKey: .scheduled)
        forecast = try? c.decodeIfPresent(Forecast.self, forKey: .forecast)
        confidence = try? c.decodeIfPresent(Confidence.self, forKey: .confidence)
        labor = (try? c.decodeIfPresent(LaborPlan.self, forKey: .labor)) ?? nil
        overtime = (try? c.decodeIfPresent(Overtime.self, forKey: .overtime)) ?? nil
    }
}

struct DSRYesterday: Decodable, Hashable {
    struct Item: Decodable, Hashable, Identifiable {
        let key: String
        let text: String
        let outcome: String?
        let actualText: String?
        var id: String { key }
        enum CodingKeys: String, CodingKey { case key, text, outcome; case actualText = "actual_text" }
    }
    struct Accuracy: Decodable, Hashable {
        let pct: Int?
        let correct: Int
        let graded: Int
        let windowDays: Int
        let minGraded: Int?
        enum CodingKeys: String, CodingKey {
            case pct, correct, graded
            case windowDays = "window_days"
            case minGraded = "min_graded"
        }
    }
    let items: [Item]
    let accuracy: Accuracy?
}

// MARK: - Views

private func toneColor(_ tone: String?) -> Color? {
    switch tone {
    case "good": return .cavnarGreen
    case "warn": return .cavnarAmber
    case "bad": return .cavnarRed
    default: return nil
    }
}

/// A small trend line — the last nights, the latest lit in ember.
struct DSRSparkline: View {
    let values: [Double]
    var body: some View {
        GeometryReader { geo in
            let lo = values.min() ?? 0, hi = values.max() ?? 1, span = max(hi - lo, 0.0001)
            let pts = values.enumerated().map { i, v in
                CGPoint(x: geo.size.width * CGFloat(i) / CGFloat(max(values.count - 1, 1)),
                        y: geo.size.height - geo.size.height * CGFloat((v - lo) / span))
            }
            Path { p in
                guard let first = pts.first else { return }
                p.move(to: first)
                pts.dropFirst().forEach { p.addLine(to: $0) }
            }
            .stroke(Color.cavnarInk3, style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
            if let last = pts.last {
                Circle().fill(Color.cavnarEmber).frame(width: 5, height: 5).position(last)
            }
        }
        .frame(width: 64, height: 20)
        .accessibilityHidden(true)
    }
}

/// The night's key numbers as slim tiles (10/8/26): label, value, trend
/// line and its change; the streak, target, fair peer comparison and note
/// open on a tap. A peer comparison that isn't fair yet is not mentioned.
struct DSRKPIGrid: View {
    var kicker: String? = nil
    let title: String
    let kpis: [DSRKPI]

    var body: some View {
        if !kpis.isEmpty {
            DSRSectionTitle(kicker: kicker, title: title)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10, alignment: .top),
                                GridItem(.flexible(), spacing: 10, alignment: .top)], spacing: 10) {
                ForEach(kpis) { DSRKPITile(kpi: $0) }
            }
        }
    }
}

struct DSRKPITile: View {
    let kpi: DSRKPI
    @State private var open = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The fair peer comparison, or nil — "no fair comparison yet" is never said.
    private var peerLine: String? {
        guard let p = kpi.peers, p.available, let v = p.valueText else { return nil }
        return "\(p.label) \(v)"
    }

    private var hasMore: Bool {
        kpi.streak != nil || kpi.detail != nil || kpi.target != nil || peerLine != nil
    }

    var body: some View {
        if hasMore {
            Button {
                Haptic.light()
                if reduceMotion { open.toggle() } else { withAnimation(.easeOut(duration: 0.2)) { open.toggle() } }
            } label: { content.contentShape(Rectangle()) }
            .buttonStyle(.plain)
            .accessibilityHint(open ? "Hides the target and trend" : "Shows the target and trend")
        } else {
            content
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                CavnarKicker(kpi.label + (kpi.estimate ? " · est." : ""), isHeader: false)
                    .lineLimit(2)
                    .minimumScaleFactor(0.85)
                Spacer(minLength: 0)
                if hasMore {
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarInk3)
                        .rotationEffect(.degrees(open ? 180 : 0))
                        .accessibilityHidden(true)
                }
            }
            HStack(alignment: .center) {
                Text(kpi.valueText).cavnarText(.figureM)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                Spacer(minLength: 4)
                if kpi.spark.count >= 3 { DSRSparkline(values: kpi.spark) }
            }
            if let c = kpi.change {
                HomeMixedText.make(c.text, role: .caption, color: toneColor(c.tone) ?? .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if open {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    if let s = kpi.streak {
                        HomeMixedText.make(s.text, role: .caption, color: toneColor(s.tone) ?? .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let t = kpi.target {
                        HomeMixedText.make("\(t.label) \(t.valueText)", role: .caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let peer = peerLine {
                        HomeMixedText.make(peer, role: .caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let d = kpi.detail {
                        HomeMixedText.make(d, role: .caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .transition(reduceMotion ? .identity : .opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.cavnarInk3.opacity(0.07)))
        .accessibilityElement(children: .combine)
    }
}

struct DSRShiftCard: View {
    let shift: DSRShift
    /// The report night's weekday, so the kicker reads "Monday's shift".
    var dayName: String? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(dayName.map { "\($0)'s shift" } ?? "The night's shift")
            if let verdict = shift.verdict, !verdict.isEmpty {
                CavnarMixedText(verdict, role: .lead)
            }
            // Two columns (re-audit D21): a FigureM in a third of a phone's
            // width truncated.
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10, alignment: .top),
                                GridItem(.flexible(), spacing: 10, alignment: .top)], spacing: 10) {
                ForEach(shift.rows) { r in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(r.valueText).cavnarText(.figureM, color: toneColor(r.tone) ?? .cavnarInk)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                        Text(r.label).cavnarText(.caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if let note = shift.note {
                Text(note).cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}

/// Cavnar AI's insights as full-width rows in one card (10/8/26: half-width
/// prose was a column of six-word lines), three lines each, the rest on tap.
struct DSRInsightsGrid: View {
    let insights: [DSRInsight]
    var body: some View {
        if !insights.isEmpty {
            DSRSectionTitle(title: "What Cavnar AI noticed")
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(insights.enumerated()), id: \.element.id) { i, x in
                    DSRInsightRow(insight: x)
                    if i < insights.count - 1 { AccountRowDivider() }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }
}

struct DSRInsightRow: View {
    let insight: DSRInsight
    @State private var open = false
    /// Under this many characters three lines hold it all; no tap needed.
    static let clampAfter = 140

    private var clamps: Bool { insight.text.count > Self.clampAfter }

    var body: some View {
        let content = VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker(insight.label)
            HomeMixedText.make(insight.text, role: .body)
                .cavnarText(.body)
                .lineLimit(open || !clamps ? nil : 3)
                .fixedSize(horizontal: false, vertical: true)
            if clamps {
                Text(open ? "Show less" : "Read more")
                    .cavnarText(.label, color: .cavnarEmber2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, CavnarSpace.s)
        .contentShape(Rectangle())

        if clamps {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.2)) { open.toggle() }
            } label: { content }
            .buttonStyle(.plain)
        } else {
            content
        }
    }
}

/// The day after: what to prep, Cavnar AI's forecast with how sure it is
/// on one line under it ("70% confident"), and what the forecast rests on
/// behind a tap (10/8/26). The staffing priority is pointed to, never said
/// a second time (the web's rule).
struct DSRTomorrowCard: View {
    let tomorrow: DSRTomorrow
    /// "Staffing for Friday: see priority 2 above" — nil when there is none.
    var staffingPointer: String? = nil

    @State private var showingBasis = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// "70% confident", or — with no figure (too little measured) — said so,
    /// never 0%.
    static func confidenceLine(_ cf: DSRTomorrow.Confidence) -> String {
        if let pct = cf.pct { return "\(pct)% confident" }
        return "No confidence figure yet"
    }

    private var basisLines: [(String, Color)] {
        var out: [(String, Color)] = []
        if let b = tomorrow.forecast?.basis, !b.isEmpty {
            // The server's basis names each measured effect it applied
            // ("Rain −12% (measured 5 times here)", M5).
            out.append((String(b.prefix(1).uppercased() + b.dropFirst()), .cavnarInk2))
        }
        if let cf = tomorrow.confidence {
            if !cf.basedOn.isEmpty { out.append(("Based on " + cf.basedOn.joined(separator: " · "), .cavnarInk2)) }
            if !cf.watch.isEmpty { out.append(("Watch: " + cf.watch.joined(separator: " · "), .cavnarInk2)) }
            if !cf.missing.isEmpty { out.append(("Missing: " + cf.missing.joined(separator: " · "), .cavnarInk2)) }
            if let t = cf.track, !t.isEmpty { out.append((t, .cavnarInk2)) }
        }
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker(tomorrow.heading)
            if let fc = tomorrow.forecast {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Cavnar AI\u{2019}s forecast").cavnarText(.secondary)
                    HomeMixedText.make(fc.text, role: .figureM)
                        .lineLimit(2)
                        .minimumScaleFactor(0.85)
                    if let cf = tomorrow.confidence {
                        Text(Self.confidenceLine(cf)).cavnarText(.secondary)
                    }
                }
            } else if let cf = tomorrow.confidence {
                Text(Self.confidenceLine(cf)).cavnarText(.secondary)
            }
            if tomorrow.items.isEmpty {
                Text("Nothing on the books to prep for.").cavnarText(.secondary)
            }
            ForEach(tomorrow.items) { item in
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                    Image(systemName: item.tone == "warn" ? "exclamationmark.triangle.fill" : "circle.fill")
                        .font(item.tone == "warn" ? .cavnar(.secondary) : .system(size: 6, weight: .bold))
                        .foregroundStyle(item.tone == "warn" ? Color.cavnarAmber : Color.cavnarInk3)
                        .frame(width: 18).accessibilityHidden(true)
                    CavnarMixedText(item.text, role: .body)
                    Spacer(minLength: 0)
                }
            }
            if let n = tomorrow.scheduled {
                HomeMixedText.make("\(n) on the published schedule", role: .secondary)
            }
            if let pointer = staffingPointer {
                HomeMixedText.make(pointer, role: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            let lines = basisLines
            if !lines.isEmpty {
                Button {
                    Haptic.light()
                    if reduceMotion { showingBasis.toggle() } else {
                        withAnimation(.easeOut(duration: 0.2)) { showingBasis.toggle() }
                    }
                } label: {
                    HStack(spacing: CavnarSpace.xxs + 2) {
                        Text(showingBasis ? "Hide what it rests on" : "What the forecast rests on")
                            .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        Image(systemName: "chevron.down")
                            .font(.cavnar(.caption))
                            .rotationEffect(.degrees(showingBasis ? 180 : 0))
                            .accessibilityHidden(true)
                        Spacer(minLength: 0)
                    }
                    .foregroundStyle(Color.cavnarEmber2)
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .accessibilityValue(showingBasis ? "Expanded" : "Collapsed")
                if showingBasis {
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                            HomeMixedText.make(line.0, role: .caption, color: line.1)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .transition(reduceMotion ? .identity : .opacity)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}

/// "Yesterday's calls: 3 of 4 right" — one line that opens the graded
/// predictions in a sheet (10/8/26: a whole card at the foot of the report).
struct DSRYesterdayLine: View {
    let yesterday: DSRYesterday
    @State private var showing = false

    /// "Yesterday's calls: 3 of 4 right", or "not graded yet" when none was.
    static func summary(_ y: DSRYesterday) -> String {
        let graded = y.items.filter { $0.outcome == "correct" || $0.outcome == "incorrect" }
        guard !graded.isEmpty else { return "Yesterday\u{2019}s calls: not graded yet" }
        let right = graded.filter { $0.outcome == "correct" }.count
        return "Yesterday\u{2019}s calls: \(right) of \(graded.count) right"
    }

    var body: some View {
        Button {
            Haptic.light()
            showing = true
        } label: {
            HStack(spacing: CavnarSpace.s) {
                Image(systemName: "scope")
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
                HomeMixedText.make(Self.summary(yesterday), role: .label)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .accessibilityHidden(true)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens how each of yesterday's predictions turned out")
        .sheet(isPresented: $showing) {
            NavigationStack {
                ScrollView {
                    DSRYesterdayCard(yesterday: yesterday)
                        .padding(CavnarSpace.gutter)
                }
                .accountSheetChrome("Yesterday\u{2019}s calls")
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
    }
}

/// How each of yesterday's predictions turned out, and the running accuracy.
struct DSRYesterdayCard: View {
    let yesterday: DSRYesterday

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            ForEach(yesterday.items) { x in
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                    Image(systemName: x.outcome == "correct" ? "checkmark.circle.fill"
                          : (x.outcome == "incorrect" ? "xmark.circle.fill" : "circle"))
                        .font(.cavnar(.body))
                        .foregroundStyle(x.outcome == "correct" ? Color.cavnarGreen
                                         : (x.outcome == "incorrect" ? Color.cavnarRed : Color.cavnarInk3))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        CavnarMixedText(x.text, role: .body)
                        HomeMixedText.make((x.outcome == "correct" ? "Correct" : (x.outcome == "incorrect" ? "Incorrect" : "Not graded"))
                                           + (x.actualText.map { " · \($0)" } ?? ""), role: .caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                }
                .accessibilityElement(children: .combine)
            }
            if let a = yesterday.accuracy {
                Divider()
                HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.s) {
                    CavnarKicker("Prediction accuracy")
                    Spacer()
                    if let pct = a.pct {
                        Text("\(pct)%").cavnarText(.figureM)
                    }
                }
                HomeMixedText.make(a.pct != nil ? "\(a.correct) of \(a.graded) right, last \(a.windowDays) days"
                                   : "\(a.correct) of \(a.graded) right so far — a percentage after \(a.minGraded ?? 5) graded",
                                   role: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}
