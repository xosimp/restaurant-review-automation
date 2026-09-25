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
}

struct DSRInsight: Decodable, Hashable, Identifiable {
    let kind: String
    let label: String
    let text: String
    var id: String { kind + text }
}

struct DSRTomorrow: Decodable, Hashable {
    struct Item: Decodable, Hashable, Identifiable {
        let kind: String?
        let tone: String?
        let text: String
        var id: String { (kind ?? "") + text }
    }
    struct Forecast: Decodable, Hashable { let text: String; let basis: String? }
    struct Confidence: Decodable, Hashable {
        let pct: Int
        let basedOn: [String]
        let missing: [String]
        let track: String?
        enum CodingKeys: String, CodingKey { case pct, missing, track; case basedOn = "based_on" }
    }
    let date: String?
    let weekday: String?
    let items: [Item]
    let scheduled: Int?
    let forecast: Forecast?
    let confidence: Confidence?

    enum CodingKeys: String, CodingKey { case date, weekday, items, scheduled, forecast, confidence }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = try? c.decodeIfPresent(String.self, forKey: .date)
        weekday = try? c.decodeIfPresent(String.self, forKey: .weekday)
        items = (try? c.decodeIfPresent([Item].self, forKey: .items)) ?? []
        scheduled = try? c.decodeIfPresent(Int.self, forKey: .scheduled)
        forecast = try? c.decodeIfPresent(Forecast.self, forKey: .forecast)
        confidence = try? c.decodeIfPresent(Confidence.self, forKey: .confidence)
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

struct DSRKPIGrid: View {
    let kicker: String
    let title: String
    let kpis: [DSRKPI]

    var body: some View {
        if !kpis.isEmpty {
            HomeSectionHeader(kicker: kicker, title: title).padding(.top, 8)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10, alignment: .top),
                                GridItem(.flexible(), spacing: 10, alignment: .top)], spacing: 10) {
                ForEach(kpis) { tile($0) }
            }
        }
    }

    private func tile(_ k: DSRKPI) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(k.label.uppercased() + (k.estimate ? " · EST." : ""))
                .font(.cavnarBody(10.5, weight: 700)).tracking(1.1).foregroundStyle(Color.cavnarInk3)
            HStack(alignment: .center) {
                Text(k.valueText).font(.cavnarNumber(22, weight: 600)).foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 4)
                if k.spark.count >= 3 { DSRSparkline(values: k.spark) }
            }
            if let c = k.change {
                HomeMixedText.make(c.text, size: 12.5, weight: 600, color: toneColor(c.tone) ?? .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let s = k.streak {
                Text(s.text).font(.cavnarBody(12.5, weight: 600)).foregroundStyle(toneColor(s.tone) ?? .cavnarInk2)
            }
            if let d = k.detail {
                Text(d).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
            }
            if let t = k.target {
                HomeMixedText.make("\(t.label) \(t.valueText)", size: 12, color: .cavnarInk2)
            }
            if let p = k.peers {
                if p.available, let v = p.valueText {
                    HomeMixedText.make("\(p.label) \(v)", size: 12, color: .cavnarInk2)
                } else {
                    Text("\(p.label): no fair comparison yet").font(.cavnarBody(11.5)).foregroundStyle(Color.cavnarInk3)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.cavnarInk3.opacity(0.07)))
    }
}

struct DSRShiftCard: View {
    let shift: DSRShift
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            DSRKicker(text: "Today's shift")
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10),
                                GridItem(.flexible(), spacing: 10)], spacing: 10) {
                ForEach(shift.rows) { r in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(r.valueText).font(.cavnarNumber(22, weight: 600))
                            .foregroundStyle(toneColor(r.tone) ?? .cavnarInk)
                        Text(r.label).font(.cavnarBody(11.5)).foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if let note = shift.note {
                Text(note).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}

struct DSRInsightsGrid: View {
    let insights: [DSRInsight]
    var body: some View {
        if !insights.isEmpty {
            HomeSectionHeader(kicker: "Cavnar AI", title: "AI insights").padding(.top, 8)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10, alignment: .top),
                                GridItem(.flexible(), spacing: 10, alignment: .top)], spacing: 10) {
                ForEach(insights) { x in
                    VStack(alignment: .leading, spacing: 6) {
                        DSRKicker(text: x.label)
                        HomeMixedText.make(x.text, size: 13.5, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    .cavnarCard()
                }
            }
        }
    }
}

struct DSRTomorrowCard: View {
    let tomorrow: DSRTomorrow
    let recommendation: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            DSRKicker(text: "Tomorrow" + (tomorrow.weekday.map { " · \($0)" } ?? ""))
            if tomorrow.items.isEmpty {
                Text("Nothing on the books to prep for.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
            }
            ForEach(tomorrow.items) { item in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: item.tone == "warn" ? "exclamationmark.triangle.fill" : "circle.fill")
                        .font(.system(size: item.tone == "warn" ? 12 : 5, weight: .bold))
                        .foregroundStyle(item.tone == "warn" ? Color.cavnarAmber : Color.cavnarInk3)
                        .frame(width: 18).padding(.top, 3).accessibilityHidden(true)
                    HomeMixedText.make(item.text, size: 14.5, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
            }
            if let n = tomorrow.scheduled {
                Text("\(n) on the published schedule").font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
            }
            if let rec = recommendation {
                VStack(alignment: .leading, spacing: 4) {
                    DSRKicker(text: "AI recommendation")
                    HomeMixedText.make(rec, size: 15, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.top, 4)
            }
            if let fc = tomorrow.forecast {
                VStack(alignment: .leading, spacing: 3) {
                    DSRKicker(text: "Cavnar's forecast")
                    Text(fc.text).font(.cavnarNumber(22, weight: 600)).foregroundStyle(Color.cavnarInk)
                    if let b = fc.basis {
                        Text(b.prefix(1).uppercased() + b.dropFirst()).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                .padding(.top, 4)
            }
            if let cf = tomorrow.confidence {
                VStack(alignment: .leading, spacing: 3) {
                    DSRKicker(text: "AI confidence")
                    Text("\(cf.pct)%").font(.cavnarNumber(26, weight: 600)).foregroundStyle(Color.cavnarInk)
                    Text("Based on " + cf.basedOn.joined(separator: " · ")).font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk2).fixedSize(horizontal: false, vertical: true)
                    if !cf.missing.isEmpty {
                        Text("Missing: " + cf.missing.joined(separator: " · ")).font(.cavnarBody(12))
                            .foregroundStyle(Color.cavnarInk3).fixedSize(horizontal: false, vertical: true)
                    }
                    if let t = cf.track {
                        Text(t).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}

struct DSRYesterdayCard: View {
    let yesterday: DSRYesterday

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            DSRKicker(text: "Cavnar grading itself")
            Text("How did yesterday turn out?").font(.cavnarHeadline(19)).foregroundStyle(Color.cavnarInk)
            ForEach(yesterday.items) { x in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: x.outcome == "correct" ? "checkmark.circle.fill"
                          : (x.outcome == "incorrect" ? "xmark.circle.fill" : "circle"))
                        .foregroundStyle(x.outcome == "correct" ? Color.cavnarGreen
                                         : (x.outcome == "incorrect" ? Color.cavnarRed : Color.cavnarInk3))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        HomeMixedText.make(x.text, size: 14.5, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(x.outcome == "correct" ? "Correct" : (x.outcome == "incorrect" ? "Incorrect" : "Not graded")
                             + (x.actualText.map { " · \($0)" } ?? ""))
                            .font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk3)
                    }
                    Spacer(minLength: 0)
                }
                .accessibilityElement(children: .combine)
            }
            if let a = yesterday.accuracy {
                Divider()
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("PREDICTION ACCURACY").font(.cavnarBody(10.5, weight: 700)).tracking(1.1)
                        .foregroundStyle(Color.cavnarEmber)
                    Spacer()
                    if let pct = a.pct {
                        Text("\(pct)%").font(.cavnarNumber(24, weight: 600)).foregroundStyle(Color.cavnarInk)
                    }
                }
                Text(a.pct != nil ? "\(a.correct) of \(a.graded) right, last \(a.windowDays) days"
                     : "\(a.correct) of \(a.graded) right so far — a percentage after \(a.minGraded ?? 5) graded")
                    .font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}
