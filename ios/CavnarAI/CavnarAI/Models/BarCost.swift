import Foundation

/// GET /mobile/api/food-cost/bar — the Bar page's payload (bar_cost.build),
/// read for the phone's one Bar card (UIX-10 / F6, "Web explains. iPhone
/// decides."). Only the parts the card shows are decoded, and every one of
/// them leniently: the backend is still growing this payload, and an odd or
/// missing field empties one line, never the card.
///
/// The figure is never named here. `pourCost.label` is the payload's own
/// name for it — "Bought vs sold" when it rests on purchases, the counted
/// name only when the shelf was counted — and the card shows that label.
struct BarPayload: Decodable {
    var ok = false
    /// False: nothing to read yet (no invoices) — `whyNot` says why.
    var available = false
    var whyNot: String?
    var asOfMdy: String?
    var pourCost: BarPourCost?
    var insights: [BarInsight] = []
    var increases: [BarPriceIncrease] = []
    var feed: BarFeed?
    var counted: BarCounted?

    enum CodingKeys: String, CodingKey {
        case ok, available, insights, feed, counted
        case whyNot = "why_not"
        case asOfMdy = "as_of_mdy"
        case pourCost = "pour_cost"
        case priceWatch = "price_watch"
    }

    private struct PriceWatch: Decodable {
        var increases: [BarPriceIncrease] = []
        enum CodingKeys: String, CodingKey { case increases }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            increases = c.setupList(BarPriceIncrease.self, .increases)
        }
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.setupBool(.ok) ?? false
        available = c.setupBool(.available) ?? false
        whyNot = c.setupText(.whyNot)
        asOfMdy = c.setupText(.asOfMdy)
        pourCost = (try? c.decodeIfPresent(BarPourCost.self, forKey: .pourCost)) ?? nil
        insights = c.setupList(BarInsight.self, .insights).filter { !$0.headline.isEmpty }
        increases = ((try? c.decodeIfPresent(PriceWatch.self, forKey: .priceWatch)) ?? nil)?.increases ?? []
        feed = (try? c.decodeIfPresent(BarFeed.self, forKey: .feed)) ?? nil
        counted = (try? c.decodeIfPresent(BarCounted.self, forKey: .counted)) ?? nil
    }

    // MARK: What the card reads

    /// The one insight the card carries — the payload's own order.
    var topInsight: BarInsight? { insights.first }

    /// Home shows the card only when its top insight needs the owner.
    var wantsHome: Bool {
        guard available, let tone = topInsight?.tone else { return false }
        return tone == "bad" || tone == "warn"
    }

    /// The price rises the server marked as headline-worthy, at most three,
    /// in the server's order.
    var headlineIncreases: [BarPriceIncrease] {
        Array(increases.filter { $0.headline }.prefix(3))
    }

    /// Whether the four-week figure is judged at all. Either flag false is
    /// not judged — the figure is what's on file, with no tone on it.
    var isJudged: Bool {
        guard let p = pourCost else { return false }
        return p.judged != false && p.total?.judged != false
    }

    static let notJudgedFallback = "Not judged yet \u{2014} the figures are what\u{2019}s on file so far."

    /// The sentence that replaces any tone when the figure isn't judged.
    var notJudgedText: String? {
        guard !isJudged else { return nil }
        return pourCost?.total?.notJudgedText ?? Self.notJudgedFallback
    }

    /// The amber lines: invoices missing for a stretch, or a feed gone
    /// quiet. Gaps first (they name the distributor), then the stale feed.
    var feedLines: [String] {
        var out: [String] = []
        for g in pourCost?.gaps ?? [] {
            guard let d = g.distributor else { continue }
            if let a = g.fromMdy, let b = g.toMdy {
                out.append("Invoices from \(d) aren\u{2019}t on file for \(a == b ? a : "\(a)\u{2013}\(b)")")
            } else {
                out.append("Some invoices from \(d) aren\u{2019}t on file")
            }
        }
        let stale = pourCost?.freshness?.stale == true || feed?.staleDays != nil
            || pourCost?.freshness?.staleDays != nil
        if stale, let through = pourCost?.freshness?.invoicesThroughMdy ?? feed?.invoicesThroughMdy {
            out.append("No invoices since \(through)")
        }
        return out
    }
}

/// The four-week reading: what it rests on, its name and its total.
struct BarPourCost: Decodable {
    /// "purchases" or "counted".
    var basis: String?
    /// "Bought vs sold" / "Pour cost (counted)" — shown as given.
    var label: String?
    var sub: String?
    var available: Bool?
    var whyNot: String?
    var judged: Bool?
    var notJudged: String?
    var total: Total?
    var window: Window?
    var freshness: Freshness?
    var gaps: [Gap] = []

    enum CodingKeys: String, CodingKey {
        case basis, label, sub, available, judged, total, window, freshness, gaps
        case whyNot = "why_not"
        case notJudged = "not_judged"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        basis = c.setupText(.basis)
        label = c.setupText(.label)
        sub = c.setupText(.sub)
        available = c.setupBool(.available)
        whyNot = c.setupText(.whyNot)
        judged = c.setupBool(.judged)
        notJudged = c.setupText(.notJudged)
        total = (try? c.decodeIfPresent(Total.self, forKey: .total)) ?? nil
        window = (try? c.decodeIfPresent(Window.self, forKey: .window)) ?? nil
        freshness = (try? c.decodeIfPresent(Freshness.self, forKey: .freshness)) ?? nil
        gaps = c.setupList(Gap.self, .gaps)
    }

    struct Total: Decodable {
        var pct: Double?
        var judged: Bool?
        var notJudgedText: String?
        var target: Double?
        var targetLabel: String?
        /// "good" / "warn" / "bad" when judged against something; nil otherwise.
        var tone: String?
        var rangeTone: String?

        enum CodingKeys: String, CodingKey {
            case pct, judged, target, tone
            case notJudgedText = "not_judged_text"
            case targetLabel = "target_label"
            case rangeTone = "range_tone"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            pct = c.setupDouble(.pct)
            judged = c.setupBool(.judged)
            notJudgedText = c.setupText(.notJudgedText)
            target = c.setupDouble(.target)
            targetLabel = c.setupText(.targetLabel)
            tone = c.setupText(.tone)
            rangeTone = c.setupText(.rangeTone)
        }
    }

    struct Window: Decodable {
        var fromMdy: String?
        var toMdy: String?
        var days: Int?
        enum CodingKeys: String, CodingKey { case days; case fromMdy = "from_mdy"; case toMdy = "to_mdy" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            fromMdy = c.setupText(.fromMdy)
            toMdy = c.setupText(.toMdy)
            days = c.setupInt(.days)
        }
    }

    struct Freshness: Decodable {
        var stale: Bool?
        var staleDays: Int?
        var invoicesThroughMdy: String?
        enum CodingKeys: String, CodingKey {
            case stale
            case staleDays = "stale_days"
            case invoicesThroughMdy = "invoices_through_mdy"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            stale = c.setupBool(.stale)
            staleDays = c.setupInt(.staleDays)
            invoicesThroughMdy = c.setupText(.invoicesThroughMdy)
        }
    }

    /// A stretch a distributor's invoices don't cover.
    struct Gap: Decodable {
        var distributor: String?
        var fromMdy: String?
        var toMdy: String?
        var categories: [String] = []
        enum CodingKeys: String, CodingKey {
            case distributor, categories
            case fromMdy = "from_mdy"
            case toMdy = "to_mdy"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            distributor = c.setupText(.distributor)
            fromMdy = c.setupText(.fromMdy)
            toMdy = c.setupText(.toMdy)
            categories = c.setupTexts(.categories)
        }
    }
}

/// One of the payload's measured insights.
struct BarInsight: Decodable, Identifiable {
    var key: String = ""
    /// "good" / "warn" / "bad" / "info".
    var tone: String?
    var headline: String = ""
    var metric: String?
    var action: String?
    var basis: String?
    var evidencePct: Int?
    var freshnessPct: Int?

    var id: String { key.isEmpty ? headline : key }

    enum CodingKeys: String, CodingKey { case key, tone, headline, metric, action, basis, confidence }
    private enum ConfidenceKeys: String, CodingKey {
        case evidencePct = "evidence_pct"
        case freshnessPct = "freshness_pct"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = c.setupText(.key) ?? ""
        tone = c.setupText(.tone)
        headline = c.setupText(.headline) ?? ""
        metric = c.setupText(.metric)
        action = c.setupText(.action)
        basis = c.setupText(.basis)
        if let conf = try? c.nestedContainer(keyedBy: ConfidenceKeys.self, forKey: .confidence) {
            evidencePct = conf.setupInt(.evidencePct)
            freshnessPct = conf.setupInt(.freshnessPct)
        }
    }

    /// The web's toned kicker words (`.br-in`).
    var kicker: String {
        switch tone {
        case "bad": return "Needs attention"
        case "warn": return "Worth a look"
        case "good": return "Going well"
        default: return "Good to know"
        }
    }

    /// "Evidence 100% · Freshness 75%" — measured percentages only.
    var confidenceLine: String? {
        var parts: [String] = []
        if let e = evidencePct { parts.append("Evidence \(e)%") }
        if let f = freshnessPct { parts.append("Freshness \(f)%") }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }
}

/// One price that went up (bar.prices).
struct BarPriceIncrease: Decodable, Identifiable {
    var productKey: String = ""
    var name: String = ""
    var distributor: String?
    var unit: String?
    var from: Double?
    var to: Double?
    var pct: Double?
    var kind: String?
    var persistent: Bool?
    var headline = false
    var forwardMonthlyUsd: Double?
    var sinceMdy: String?
    var explain: String?

    var id: String { productKey.isEmpty ? name : productKey }

    enum CodingKeys: String, CodingKey {
        case name, product, distributor, unit, from, to, pct, kind, persistent, headline, explain
        case productKey = "product_key"
        case forwardMonthlyUsd = "forward_monthly_usd"
        case sinceMdy = "since_mdy"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        productKey = c.setupText(.productKey) ?? ""
        name = c.setupText(.name) ?? c.setupText(.product) ?? ""
        distributor = c.setupText(.distributor)
        unit = c.setupText(.unit)
        from = c.setupDouble(.from)
        to = c.setupDouble(.to)
        pct = c.setupDouble(.pct)
        kind = c.setupText(.kind)
        persistent = c.setupBool(.persistent)
        headline = c.setupBool(.headline) ?? false
        forwardMonthlyUsd = c.setupDouble(.forwardMonthlyUsd)
        sinceMdy = c.setupText(.sinceMdy)
        explain = c.setupText(.explain)
    }

    /// "+$522 a month" at the usual buying; "+10%" when the server sent no
    /// money figure. Nil when it sent neither.
    var impactLine: String? {
        if let usd = forwardMonthlyUsd, usd > 0 { return "+$\(usd.commaFormatted) a month" }
        if let pct, pct > 0 { return "+\(pct == pct.rounded() ? String(Int(pct)) : String(format: "%.1f", pct))%" }
        return nil
    }
}

/// How far the Fintech feed and the nightly sales reach.
struct BarFeed: Decodable {
    var invoicesThroughMdy: String?
    var salesThroughMdy: String?
    var lastFileMdy: String?
    var staleDays: Int?
    enum CodingKeys: String, CodingKey {
        case invoicesThroughMdy = "invoices_through_mdy"
        case salesThroughMdy = "sales_through_mdy"
        case lastFileMdy = "last_file_mdy"
        case staleDays = "stale_days"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        invoicesThroughMdy = c.setupText(.invoicesThroughMdy)
        salesThroughMdy = c.setupText(.salesThroughMdy)
        lastFileMdy = c.setupText(.lastFileMdy)
        staleDays = c.setupInt(.staleDays)
    }
}

/// The bar's shelf counts (bar.counts).
struct BarCounted: Decodable {
    var available: Bool?
    var lastCountMdy: String?
    var nextDueMdy: String?
    var missing: [Missing] = []

    struct Missing: Decodable {
        var component: String?
        var why: String?
        enum CodingKeys: String, CodingKey { case component, why }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            component = c.setupText(.component)
            why = c.setupText(.why)
        }
    }

    enum CodingKeys: String, CodingKey {
        case available, missing
        case lastCountMdy = "last_count_mdy"
        case nextDueMdy = "next_due_mdy"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.setupBool(.available)
        lastCountMdy = c.setupText(.lastCountMdy)
        nextDueMdy = c.setupText(.nextDueMdy)
        missing = c.setupList(Missing.self, .missing)
    }

    /// "Last counted 9/30/26 · next count due 10/31/26".
    var line: String? {
        var parts: [String] = []
        if let l = lastCountMdy { parts.append("Last counted \(l)") }
        if let n = nextDueMdy { parts.append(parts.isEmpty ? "Next count due \(n)" : "next count due \(n)") }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }
}

// MARK: - Count the bar (POST /mobile/api/food-cost/bar/counts)

/// The count the owner typed: a value per category, at what was paid.
/// Only the categories typed are sent — a blank is never sent as 0.
struct BarCountBody: Encodable, Equatable {
    let countDate: String
    let values: [String: Double]
    let note: String?

    enum CodingKeys: String, CodingKey {
        case values, note
        case countDate = "count_date"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(countDate, forKey: .countDate)
        try c.encode(values, forKey: .values)
        if let note { try c.encode(note, forKey: .note) }
    }

    /// One category's typed value, read for the device's locale.
    enum Entry: Equatable {
        case blank
        case value(Double)
        case invalid
    }

    static func entry(_ typed: String, locale: Locale = .current) -> Entry {
        let t = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { return .blank }
        guard let v = NumberFormatter.cavnarParse(t, locale: locale), v >= 0 else { return .invalid }
        return .value(v)
    }

    /// The body for what was typed, or the sentence to show instead.
    static func make(date: String, beer: String, liquor: String, wine: String, note: String,
                     locale: Locale = .current) -> Result<BarCountBody, BarCountError> {
        var values: [String: Double] = [:]
        for (key, typed) in [("beer", beer), ("liquor", liquor), ("wine", wine)] {
            switch entry(typed, locale: locale) {
            case .blank: continue
            case .invalid: return .failure(BarCountError(field: key, message: "Enter an amount in dollars."))
            case .value(let v): values[key] = v
            }
        }
        if values.isEmpty {
            return .failure(BarCountError(field: nil, message: "Type a value for beer, liquor or wine."))
        }
        let n = note.trimmingCharacters(in: .whitespacesAndNewlines)
        return .success(BarCountBody(countDate: date, values: values, note: n.isEmpty ? nil : n))
    }
}

struct BarCountError: Error, Equatable {
    /// The category whose box is wrong; nil for the form as a whole.
    let field: String?
    let message: String
}

/// {ok, count_date_mdy, saved} or {ok: false, error}.
struct BarCountResponse: Decodable {
    var ok = false
    var error: String?
    var countDateMdy: String?
    var saved: [String] = []
    enum CodingKeys: String, CodingKey {
        case ok, error, saved
        case countDateMdy = "count_date_mdy"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.setupBool(.ok) ?? false
        error = c.setupText(.error)
        countDateMdy = c.setupText(.countDateMdy)
        saved = c.setupTexts(.saved)
    }
}
