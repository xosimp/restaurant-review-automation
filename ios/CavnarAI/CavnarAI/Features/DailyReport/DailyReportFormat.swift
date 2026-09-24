import SwiftUI

/// How every figure on the Daily Report reads. One place, so a null reads
/// "—" everywhere and never "$0", "0%" or a blank (DESIGN_SYSTEM.md §10:
/// unknown is never zero).
enum DSRFormat {
    static let dash = "\u{2014}"
    private static let minus = "\u{2212}"

    /// "$6,975" — cents only below $1,000 when there are cents ("$176.40").
    static func money(_ v: Double?) -> String {
        guard let v else { return dash }
        let sign = v < 0 ? minus : ""
        return sign + "$" + magnitude(abs(v))
    }

    /// "+$525" / "−$135".
    static func signedMoney(_ v: Double?) -> String {
        guard let v else { return dash }
        return (v < 0 ? minus : "+") + "$" + magnitude(abs(v))
    }

    /// "27.4%".
    static func pct(_ v: Double?) -> String {
        guard let v else { return dash }
        return trimmed(v, digits: 1) + "%"
    }

    /// "+8.1%" / "−1.9%".
    static func signedPct(_ v: Double?) -> String {
        guard let v else { return dash }
        return (v < 0 ? minus : "+") + trimmed(abs(v), digits: 1) + "%"
    }

    /// "+1.4 pts".
    static func signedPoints(_ v: Double?) -> String {
        guard let v else { return dash }
        return (v < 0 ? minus : "+") + trimmed(abs(v), digits: 1) + " pts"
    }

    /// "212", "1,840", "124.5".
    static func count(_ v: Double?) -> String {
        guard let v else { return dash }
        if v == v.rounded() { return v.commaFormatted }
        return trimmed(v, digits: 1)
    }

    /// "4.0★".
    static func rating(_ v: Double?) -> String {
        guard let v else { return dash }
        return String(format: "%.1f", v) + "\u{2605}"
    }

    /// "71°".
    static func degrees(_ v: Double?) -> String {
        guard let v else { return dash }
        return "\(Int(v.rounded()))\u{00B0}"
    }

    /// The POS's hour ("18") as the axis reads it: "6p", "12p", "11a".
    static func hourLabel(_ raw: String) -> String {
        guard let h = Int(raw.prefix(2).trimmingCharacters(in: .whitespaces)) else { return raw }
        let h12 = h % 12 == 0 ? 12 : h % 12
        return "\(h12)\(h < 12 || h == 24 ? "a" : "p")"
    }

    /// A restaurant-local stamp ("2026-09-23T20:01", access._stamp_local) as
    /// the time alone: "8:01pm". Nil when there isn't one.
    static func localTime(_ stamp: String?) -> String? {
        guard let stamp, stamp.count >= 16 else { return nil }
        let hm = stamp.dropFirst(11).prefix(5).split(separator: ":")
        guard hm.count == 2, let h = Int(hm[0]), let m = Int(hm[1]) else { return nil }
        let h12 = h % 12 == 0 ? 12 : h % 12
        return "\(h12):\(String(format: "%02d", m))\(h < 12 ? "am" : "pm")"
    }

    /// Green when the move is good, red when it's bad, ink3 when unknown or
    /// flat. `higherIsBetter` false for labor %, food cost %, waste.
    static func tone(_ v: Double?, higherIsBetter: Bool = true) -> Color {
        guard let v, v != 0 else { return .cavnarInk3 }
        return (v > 0) == higherIsBetter ? .cavnarGreen : .cavnarRed
    }

    /// Adds `days` to an ISO date ("2026-09-16" → "2026-09-23"), on the
    /// Gregorian calendar in UTC so a DST change can't shift the day.
    static func isoAdding(days: Int, to iso: String) -> String? {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let p = iso.prefix(10).split(separator: "-").compactMap { Int($0) }
        guard p.count == 3,
              let d = cal.date(from: DateComponents(year: p[0], month: p[1], day: p[2])),
              let moved = cal.date(byAdding: .day, value: days, to: d) else { return nil }
        let c = cal.dateComponents([.year, .month, .day], from: moved)
        guard let y = c.year, let m = c.month, let dd = c.day else { return nil }
        return String(format: "%04d-%02d-%02d", y, m, dd)
    }

    /// Whether a push payload's date is a real YYYY-MM-DD before it goes
    /// anywhere near a URL path. Shape validation only — the server decides
    /// whose night it is.
    static func isISODate(_ s: String?) -> Bool {
        guard let s, s.count == 10 else { return false }
        let parts = s.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].count == 4, parts[1].count == 2, parts[2].count == 2,
              parts.allSatisfy({ $0.allSatisfy(\.isASCII) && $0.allSatisfy(\.isNumber) }),
              let m = Int(parts[1]), let d = Int(parts[2]) else { return false }
        return (1...12).contains(m) && (1...31).contains(d)
    }

    private static func magnitude(_ v: Double) -> String {
        if v >= 1000 || v == v.rounded() { return v.commaFormatted }
        return String(format: "%.2f", v)
    }

    private static func trimmed(_ v: Double, digits: Int) -> String {
        let s = String(format: "%.\(digits)f", v)
        return s.hasSuffix(".0") ? String(s.dropLast(2)) : s
    }
}

// MARK: - Reading a block's detail

/// Typed reads of `DSRBlock.detail`, one per thing the report shows. Each
/// returns empty or nil when the payload doesn't carry it — so a section
/// with nothing to show is simply not drawn.
extension DSRBlock {
    struct Hour: Hashable { let label: String; let net: Double? }
    struct Category: Hashable { let name: String; let net: Double?; let share: Double? }
    struct Item: Hashable { let name: String; let qty: Double?; let net: Double? }
    struct Field: Hashable { let key: String; let label: String; let text: String }
    struct ReviewRow: Hashable { let rating: Double?; let summary: String; let urgent: Bool; let sentiment: String? }
    struct StockRow: Hashable { let item: String; let daysRemaining: Double?; let unit: String? }
    struct VarianceRow: Hashable { let ingredient: String; let cost: Double?; let dish: String? }
    struct PostRow: Hashable { let topic: String; let platform: String?; let at: String?; let reach: Double? }
    struct TextRow: Hashable { let message: String; let sent: Double? }

    // Sales

    var hourly: [Hour] {
        detail["hourly"]?.array.compactMap { h in
            guard let raw = h["hour"]?.string ?? h["hour"]?.int.map(String.init) else { return nil }
            return Hour(label: DSRFormat.hourLabel(raw), net: h["net"]?.double)
        } ?? []
    }

    var categories: [Category] {
        let fromDetail = detail["categories"]?.array.compactMap { c -> Category? in
            guard let name = c["category"]?.string else { return nil }
            return Category(name: name, net: c["net"]?.double, share: c["share_pct"]?.double)
        } ?? []
        if !fromDetail.isEmpty { return fromDetail }
        // Older snapshots carry the categories only as `cat:<name>` metrics.
        return metrics.keys.filter { $0.hasPrefix("cat:") }.sorted()
            .map { Category(name: String($0.dropFirst(4)), net: metric($0), share: nil) }
    }

    var topItems: [Item] { items("top_items") }

    // What gross and net mean for this restaurant (`detail.definition`,
    // restaurants.dsr_gross_basis): "items" = items at the price rung, no
    // tax; "all" = everything rung, tax and voided lines included. Net is
    // the same figure either way.

    private var definition: JSONValue? { detail["definition"] }

    /// "items" or "all"; nil from an older report that doesn't say.
    var grossBasis: String? { definition?["gross_basis"]?.string }

    /// The Tax tile's caption: tax is inside an "all" gross, never in net.
    var taxCaption: String {
        grossBasis == "all" ? "in gross, not net" : "not in gross or net"
    }

    /// "Gross is items at the price rung, … Net is gross less discounts
    /// and comps." — the server's own definitions. `net_deductions` is
    /// the fallback only for a report that carries no `net` sentence.
    var definitionNote: String? {
        var parts: [String] = []
        if let gross = definition?["gross"]?.string { parts.append("Gross is \(gross).") }
        if let net = definition?["net"]?.string {
            parts.append("Net is \(net).")
        } else {
            let takes = (definition?["net_deductions"]?.array ?? []).compactMap(\.string)
            if !takes.isEmpty { parts.append("Net takes off \(takes.joined(separator: " and ")).") }
        }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    /// Where the gross figure would be, when an "all" gross could not be
    /// measured: "Gross not measured — the POS didn't report tax and
    /// voids". Nil when gross was measured or nothing is named missing.
    var grossNotMeasured: String? {
        guard metric("gross") == nil else { return nil }
        let missing = (definition?["gross_missing"]?.array ?? []).compactMap(\.string)
        guard !missing.isEmpty else { return nil }
        return "Gross not measured \u{2014} the POS didn\u{2019}t report \(missing.joined(separator: " and "))"
    }

    private func items(_ key: String) -> [Item] {
        detail[key]?.array.compactMap { i in
            guard let name = i["name"]?.string else { return nil }
            return Item(name: name, qty: i["qty"]?.double, net: i["net"]?.double)
        } ?? []
    }

    // Labor

    var observations: [String] {
        detail["observations"]?.array.compactMap { $0["text"]?.string } ?? []
    }

    var coverageNote: String? {
        guard detail["coverage"]?["measured"]?.bool == false else { return nil }
        return detail["coverage"]?["reason"]?.string
    }

    // Food

    var estimateLabel: String? { detail["estimate"]?["label"]?.string }
    var stockBasis: String? { detail["stock"]?["basis"]?.string }

    var criticalStock: [StockRow] {
        detail["stock"]?["critical"]?.array.compactMap { s in
            guard let item = s["item"]?.string else { return nil }
            return StockRow(item: item, daysRemaining: s["days_remaining"]?.double, unit: s["unit"]?.string)
        } ?? []
    }

    var varianceItems: [VarianceRow] {
        detail["variance"]?["items"]?.array.compactMap { v in
            guard let name = v["ingredient"]?.string else { return nil }
            return VarianceRow(ingredient: name, cost: v["cost"]?.double, dish: v["dish"]?.string)
        } ?? []
    }

    var varianceWindow: String? { detail["variance"]?["window"]?.string }

    // Reviews

    var reviewRows: [ReviewRow] {
        detail["reviews"]?.array.compactMap { r in
            guard let summary = r["summary"]?.string else { return nil }
            return ReviewRow(rating: r["rating"]?.double, summary: summary,
                             urgent: r["urgent"]?.bool ?? false, sentiment: r["sentiment"]?.string)
        } ?? []
    }

    var syncNote: String? { detail["sync"]?["note"]?.string }

    // Marketing

    var posts: [PostRow] {
        detail["posts"]?["items"]?.array.compactMap { p in
            guard let topic = p["topic"]?.string else { return nil }
            return PostRow(topic: topic, platform: p["platform"]?.string, at: p["at"]?.string, reach: p["reach"]?.double)
        } ?? []
    }

    var textCampaigns: [TextRow] {
        detail["campaigns"]?["texts"]?.array.compactMap { t in
            guard let message = t["message"]?.string else { return nil }
            return TextRow(message: message, sent: t["sent"]?.double)
        } ?? []
    }

    // Intel

    var weatherSummary: String? { detail["weather"]?["summary"]?.string }
    var weatherNote: String? { detail["weather"]?["note"]?.string }
    var eventsSummary: String? { detail["events"]?["summary"]?.string }
    var competitorsNote: String? { detail["competitors"]?["note"]?.string }

    // Close-out — verbatim, in the server's order, only the fields written.

    var closeoutFields: [Field] {
        guard let fields = detail["fields"]?.object else { return [] }
        let labels = detail["labels"]?.object ?? [:]
        var order = detail["order"]?.array.compactMap(\.string) ?? []
        for key in fields.keys.sorted() where !order.contains(key) { order.append(key) }
        return order.compactMap { key in
            guard let text = fields[key]?.string?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty
            else { return nil }
            let label = labels[key]?.string ?? key.replacingOccurrences(of: "_", with: " ").capitalized
            return Field(key: key, label: label, text: text)
        }
    }

    var closeoutByline: String? {
        guard detail["filed"]?.bool == true else { return nil }
        let who = detail["submitted_by"]?.string ?? "A manager"
        if let at = detail["filed_at_label"]?.string { return "Filed by \(who) · \(at)" }
        return "Filed by \(who)"
    }

    /// "RPower", "Google", "Cavnar" — where the block came from. None for
    /// the close-out, whose byline already says who filed it.
    var sourceLabel: String? {
        guard let s = source, !s.isEmpty, s != "manager" else { return nil }
        switch s {
        case "rpower": return "RPower"
        default: return s.prefix(1).uppercased() + s.dropFirst()
        }
    }
}

// MARK: - One-line headlines

/// The one line each block's card shows collapsed — only the parts the
/// payload measured, joined with " · "; nil when it measured none of them.
enum DSRHeadline {
    static func line(for name: String, _ b: DSRBlock) -> String? {
        let parts: [String?]
        switch name {
        case "sales":
            parts = [
                b.metric("net").map { "Net \(DSRFormat.money($0))" },
                b.metric("vs_yesterday_pct").map { "\(DSRFormat.signedPct($0)) vs yesterday" },
                b.metric("vs_budget_net_pct").map { "\(DSRFormat.signedPct($0)) vs budget" },
            ]
        case "labor":
            parts = [
                b.metric("pct").map { "\(DSRFormat.pct($0)) of net sales" },
                b.metric("target_pct").map { "target \(DSRFormat.pct($0))" },
                b.metric("vs_target_pts").map { DSRFormat.signedPoints($0) },
            ]
        case "food":
            parts = [
                b.metric("est_food_cost_pct").map { "Est. food cost \(DSRFormat.pct($0))" },
                b.metric("waste_logged").map { "waste \(DSRFormat.money($0))" },
                b.metric("critical_low").flatMap { $0 > 0 ? "\(DSRFormat.count($0)) critically low" : nil },
            ]
        case "reviews":
            parts = [
                b.metric("received").map { "\(DSRFormat.count($0)) review\($0 == 1 ? "" : "s")" },
                b.metric("avg_rating").map { "\(DSRFormat.rating($0)) average" },
                b.metric("urgent").flatMap { $0 > 0 ? "\(DSRFormat.count($0)) urgent" : nil },
            ]
        case "marketing":
            parts = [
                b.metric("posts_published").map { "\(DSRFormat.count($0)) post\($0 == 1 ? "" : "s")" },
                b.metric("reach").map { "\(DSRFormat.count($0)) reach" },
                b.metric("texts_sent").map { "\(DSRFormat.count($0)) texts sent" },
            ]
        case "intel":
            parts = [b.weatherSummary, b.eventsSummary == "Nothing listed" ? nil : b.eventsSummary]
        case "closeout":
            let n = b.closeoutFields.count
            parts = [b.closeoutByline ?? (b.detail["filed"]?.bool == false ? "Not filed" : nil),
                     n > 0 ? "\(n) line\(n == 1 ? "" : "s")" : nil]
        default:
            parts = []
        }
        let kept = parts.compactMap { $0 }
        return kept.isEmpty ? nil : kept.joined(separator: " \u{00B7} ")
    }
}

extension DSRFormat {
    /// "Tuesday" for "2026-09-22".
    static func weekday(_ iso: String) -> String? {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let p = iso.prefix(10).split(separator: "-").compactMap { Int($0) }
        guard p.count == 3, let d = cal.date(from: DateComponents(year: p[0], month: p[1], day: p[2])) else { return nil }
        let names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        return names[cal.component(.weekday, from: d) - 1]
    }
}
