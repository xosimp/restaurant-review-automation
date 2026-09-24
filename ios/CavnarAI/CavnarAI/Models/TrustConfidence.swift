import Foundation

/// The one confidence object every recommendation-bearing payload carries
/// (FIXLIST contract K1): Home cards, Needs attention, the one-thing hero,
/// the review / food / labor diagnoses, food drivers, Ask answers and the
/// daily report's actions.
///
/// ```
/// {"pct": 72 | null, "band": "low"|"medium"|"high",
///  "label": "72% confidence" | "Confidence not yet measurable",
///  "reason": "...", "score": 0.72, "caution": null | "...",
///  "dimensions": {"evidence": {...}, "accuracy": {...}, "freshness": {...}},
///  "version": 1}
/// ```
///
/// Before K1 the same key was a bare band string ("high" / "medium" / "low"
/// / "moderate") on most of these payloads, and Home sent an older object
/// ({score, band, label, reason, caution}). All three decode here, and every
/// field is read leniently: an odd value is nil, never a payload that fails
/// to decode. A value that is neither a string nor an object decodes as an
/// empty confidence, which renders nothing (`ConfidenceDisplay.isRenderable`).
struct TrustConfidence: Codable, Hashable, Sendable {
    /// One of the three things the overall figure rests on.
    struct Dimension: Codable, Hashable, Sendable {
        var pct: Int?
        var basis: String?
        var n: Int?
        /// Accuracy only: how many of `n` measured results improved.
        var improved: Int?
        /// Accuracy only: "own" | "cohort" | "none".
        var source: String?
        /// Accuracy only: the likely range (Wilson 90%), percent.
        var low: Int?
        var high: Int?
        /// Freshness only: the stalest source's date, M/D/YY and ISO.
        var asOf: String?
        var asOfISO: String?
        var stalest: String?
        /// Evidence only: how many observations make a full sample, when the
        /// server sends it (confidence_engine.N_FULL).
        var nFull: Int?
        /// Accuracy only: "N% likely to beat doing nothing" — what the
        /// accuracy % means since the support-score decision (9/24/26).
        var beatsLabel: String?

        enum CodingKeys: String, CodingKey {
            case pct, basis, n, improved, source, low, high, stalest
            case asOf = "as_of"
            case asOfISO = "as_of_iso"
            case nFull = "n_full"
            case beatsLabel = "beats_label"
        }

        init(pct: Int? = nil, basis: String? = nil, n: Int? = nil, improved: Int? = nil,
             source: String? = nil, low: Int? = nil, high: Int? = nil,
             asOf: String? = nil, asOfISO: String? = nil, stalest: String? = nil, nFull: Int? = nil,
             beatsLabel: String? = nil) {
            self.pct = pct; self.basis = basis; self.n = n; self.improved = improved
            self.source = source; self.low = low; self.high = high
            self.asOf = asOf; self.asOfISO = asOfISO; self.stalest = stalest; self.nFull = nFull
            self.beatsLabel = beatsLabel
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            pct = TrustConfidence.percent(c, .pct)
            basis = TrustConfidence.text(c, .basis)
            n = TrustConfidence.integer(c, .n)
            improved = TrustConfidence.integer(c, .improved)
            source = TrustConfidence.text(c, .source)?.lowercased()
            low = TrustConfidence.percent(c, .low)
            high = TrustConfidence.percent(c, .high)
            asOf = TrustConfidence.text(c, .asOf)
            asOfISO = TrustConfidence.text(c, .asOfISO)
            stalest = TrustConfidence.text(c, .stalest)
            nFull = TrustConfidence.integer(c, .nFull)
            beatsLabel = TrustConfidence.text(c, .beatsLabel)
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encodeIfPresent(pct, forKey: .pct)
            try c.encodeIfPresent(basis, forKey: .basis)
            try c.encodeIfPresent(n, forKey: .n)
            try c.encodeIfPresent(improved, forKey: .improved)
            try c.encodeIfPresent(source, forKey: .source)
            try c.encodeIfPresent(low, forKey: .low)
            try c.encodeIfPresent(high, forKey: .high)
            try c.encodeIfPresent(asOf, forKey: .asOf)
            try c.encodeIfPresent(asOfISO, forKey: .asOfISO)
            try c.encodeIfPresent(stalest, forKey: .stalest)
            try c.encodeIfPresent(nFull, forKey: .nFull)
            try c.encodeIfPresent(beatsLabel, forKey: .beatsLabel)
        }

        /// Sample or demo data: evidence scored 0 with nothing counted
        /// (confidence_engine.evidence(sample=True)).
        var isSample: Bool {
            pct == 0 && (n ?? 0) == 0
                && (basis?.range(of: "sample|demo", options: [.regularExpression, .caseInsensitive]) != nil)
        }
    }

    /// Where the bands start, when the server sends them; else the engine's
    /// (confidence_engine.HIGH_AT / MEDIUM_AT, held in step by a test).
    struct Thresholds: Codable, Hashable, Sendable {
        var high: Int
        var medium: Int
        static let engine = Thresholds(high: 75, medium: 50)
    }

    /// The ceilings confidence_engine.overall applies, when the server sends
    /// them; else the engine's.
    struct Caps: Codable, Hashable, Sendable {
        var noTrackRecord: Int?
        var stale: Int?
        var staleBelow: Int?
        /// Group P: a record that doesn't yet beat doing nothing, one that
        /// leans against the advice, and freshness nothing could date.
        var recordUnproven: Int? = nil
        var recordAgainst: Int? = nil
        var freshnessUnmeasured: Int? = nil
        enum CodingKeys: String, CodingKey {
            case stale
            case noTrackRecord = "no_track_record"
            case staleBelow = "stale_below"
            case recordUnproven = "record_unproven"
            case recordAgainst = "record_against"
            case freshnessUnmeasured = "freshness_unmeasured"
        }
    }

    struct Dimensions: Codable, Hashable, Sendable {
        var evidence: Dimension?
        var accuracy: Dimension?
        var freshness: Dimension?

        init(evidence: Dimension? = nil, accuracy: Dimension? = nil, freshness: Dimension? = nil) {
            self.evidence = evidence; self.accuracy = accuracy; self.freshness = freshness
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            evidence = try? c.decodeIfPresent(Dimension.self, forKey: .evidence)
            accuracy = try? c.decodeIfPresent(Dimension.self, forKey: .accuracy)
            freshness = try? c.decodeIfPresent(Dimension.self, forKey: .freshness)
        }
    }

    /// The overall Recommendation Confidence, 0–100; nil when it cannot yet
    /// be measured (K1) or on a payload from before K1.
    var pct: Int?
    /// "high" | "medium" | "low" — normalised ("moderate" reads as medium).
    var band: String?
    var label: String?
    var reason: String?
    var score: Double?
    var caution: String?
    var dimensions: Dimensions?
    var version: Int?
    var thresholds: Thresholds?
    var caps: Caps?
    /// What the % means: "How well supported this is — not the chance it
    /// works" (the owner's support-score decision, 9/24/26). The Why? sheet
    /// leads with it.
    var meaning: String?
    /// The ceilings that set the figure, in the engine's order
    /// ("no_track_record", "record_unproven", "record_against", "stale",
    /// "freshness_unmeasured").
    var capsApplied: [String]?

    enum CodingKeys: String, CodingKey {
        case pct, band, label, reason, score, caution, dimensions, version, thresholds, caps, meaning
        case capsApplied = "caps_applied"
    }

    init(pct: Int? = nil, band: String? = nil, label: String? = nil, reason: String? = nil,
         score: Double? = nil, caution: String? = nil, dimensions: Dimensions? = nil, version: Int? = nil,
         thresholds: Thresholds? = nil, caps: Caps? = nil, meaning: String? = nil, capsApplied: [String]? = nil) {
        self.pct = pct.map { max(0, min(100, $0)) }
        self.band = TrustConfidence.normalisedBand(band)
        self.label = label; self.reason = reason; self.score = score
        self.caution = caution; self.dimensions = dimensions; self.version = version
        self.thresholds = thresholds; self.caps = caps
        self.meaning = meaning; self.capsApplied = capsApplied
    }

    /// A bare legacy band ("high", "moderate", …).
    init(legacyBand: String?) {
        self.init(band: legacyBand)
    }

    init(from decoder: Decoder) throws {
        // The legacy shape: a bare band string.
        if let single = try? decoder.singleValueContainer(), let s = try? single.decode(String.self) {
            self.init(legacyBand: s)
            return
        }
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else {
            // A number, an array, a bool: nothing to show, never a failed
            // payload.
            self.init()
            return
        }
        self.init(
            pct: Self.percent(c, .pct),
            band: Self.text(c, .band),
            label: Self.text(c, .label),
            reason: Self.text(c, .reason),
            score: Self.number(c, .score),
            caution: Self.text(c, .caution),
            dimensions: try? c.decodeIfPresent(Dimensions.self, forKey: .dimensions),
            version: Self.integer(c, .version),
            thresholds: (try? c.decodeIfPresent(Thresholds.self, forKey: .thresholds)) ?? nil,
            caps: (try? c.decodeIfPresent(Caps.self, forKey: .caps)) ?? nil,
            meaning: Self.text(c, .meaning),
            capsApplied: (try? c.decodeIfPresent([String].self, forKey: .capsApplied)) ?? nil
        )
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(pct, forKey: .pct)
        try c.encodeIfPresent(band, forKey: .band)
        try c.encodeIfPresent(label, forKey: .label)
        try c.encodeIfPresent(reason, forKey: .reason)
        try c.encodeIfPresent(score, forKey: .score)
        try c.encodeIfPresent(caution, forKey: .caution)
        try c.encodeIfPresent(dimensions, forKey: .dimensions)
        try c.encodeIfPresent(version, forKey: .version)
        try c.encodeIfPresent(thresholds, forKey: .thresholds)
        try c.encodeIfPresent(caps, forKey: .caps)
        try c.encodeIfPresent(meaning, forKey: .meaning)
        try c.encodeIfPresent(capsApplied, forKey: .capsApplied)
    }

    /// The object a view draws: `detail` (confidence_detail), else a
    /// `legacy` value that is itself K1. A bare band or the model's own word
    /// is not a measurement and draws nothing (B1 L1: "Low confidence" from
    /// the model's band).
    static func measured(_ detail: TrustConfidence?, _ legacy: TrustConfidence?) -> TrustConfidence? {
        if let detail, detail.isMeasuredShape { return detail }
        if let legacy, legacy.isMeasuredShape { return legacy }
        return nil
    }

    /// The K1 object (anything from the confidence engine), as against a
    /// legacy band or the older Home object.
    var isMeasuredShape: Bool { version != nil || dimensions != nil || pct != nil }

    /// The band a caller that still thinks in bands reads — the server's,
    /// else derived from the percentage the way the server derives it
    /// (≥75 high, 50–74 medium, below 50 low), else "low".
    var effectiveBand: String {
        if let band { return band }
        guard let pct else { return "low" }
        let at = thresholds ?? .engine
        return pct >= at.high ? "high" : (pct >= at.medium ? "medium" : "low")
    }

    // MARK: - Lenient readers

    static func normalisedBand(_ raw: String?) -> String? {
        guard let s = raw?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(), !s.isEmpty else { return nil }
        switch s {
        case "high", "medium", "low": return s
        case "moderate": return "medium"
        default: return nil
        }
    }

    fileprivate static func text<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> String? {
        guard let s = (try? c.decodeIfPresent(String.self, forKey: key)) ?? nil else { return nil }
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }

    fileprivate static func number<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Double? {
        if let d = (try? c.decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return d }
        return nil
    }

    fileprivate static func integer<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Int? {
        if let i = (try? c.decodeIfPresent(Int.self, forKey: key)) ?? nil { return i }
        if let d = number(c, key) { return Int(d.rounded()) }
        return nil
    }

    /// A 0–100 figure: an Int, or a Double rounded; anything else is nil.
    fileprivate static func percent<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Int? {
        integer(c, key).map { max(0, min(100, $0)) }
    }
}

/// Older call sites named the Home card's object this.
typealias HomeConfidence = TrustConfidence

// MARK: - What the owner reads

/// Everything a confidence line and its "Why?" sheet show, worked out once
/// and pure, so the wording is pinned by tests rather than by a view.
///
/// Percentages stay percentages (the owner's decision); a figure below its
/// sample floor is "—" with what it still needs, never an invented number.
struct ConfidenceDisplay: Equatable {
    enum Tone: Equatable {
        /// ≥75 — green.
        case good
        /// 50–74 — ink2.
        case neutral
        /// Below 50, or not measurable — amber. Never red, never ember.
        case warn
    }

    struct Row: Equatable {
        let title: String
        let value: String
        let tone: Tone
        let basis: String
        let detail: String?
        let meterFraction: Double?
        /// A plain-words note under the detail (the pull toward 50%).
        var note: String? = nil
    }

    let pct: Int?
    let tone: Tone
    let lineLabel: String
    let reason: String?
    let caution: String?
    let showsWhy: Bool
    let rows: [Row]
    let footer: String
    let meterFraction: Double?
    /// Whether there is anything to draw at all.
    let isRenderable: Bool
    /// What the figure means — the Why? sheet's first line (group P). The
    /// engine's words when an older server sent none.
    let meaning: String?

    static let engineMeaning = "How well supported this is \u{2014} not the chance it works"

    static let footerBase = "The overall figure combines all three \u{2014} the weakest pulls it down most."
    static let footerNoTrackRecord = " Without a track record here it stays at 70% or below."

    static func tone(pct: Int?, at: TrustConfidence.Thresholds = .engine) -> Tone {
        guard let pct else { return .warn }
        if pct >= at.high { return .good }
        if pct >= at.medium { return .neutral }
        return .warn
    }

    static func tone(band: String?) -> Tone {
        switch TrustConfidence.normalisedBand(band) {
        case "high": return .good
        case "medium": return .neutral
        default: return .warn
        }
    }

    static func percentText(_ pct: Int?) -> String { pct.map { "\($0)%" } ?? "\u{2014}" }

    static func fraction(_ pct: Int?) -> Double? { pct.map { Double(max(0, min(100, $0))) / 100 } }

    /// `9/23/26` stays; an ISO date is turned into one; nothing is never ISO.
    static func mdyDate(asOf: String?, asOfISO: String?) -> String? {
        if let a = asOf?.trimmingCharacters(in: .whitespaces), !a.isEmpty {
            if a.range(of: #"^\d{1,2}/\d{1,2}/\d{2}$"#, options: .regularExpression) != nil { return a }
            let converted = CavnarDate.mdy(a)
            if converted != a { return converted }
        }
        if let iso = asOfISO?.trimmingCharacters(in: .whitespaces), !iso.isEmpty {
            let converted = CavnarDate.mdy(iso)
            if converted != iso { return converted }
        }
        // An unparseable as_of that is not ISO-shaped is the server's own
        // words; anything ISO-shaped that failed to convert is dropped.
        if let a = asOf?.trimmingCharacters(in: .whitespaces), !a.isEmpty,
           a.range(of: #"^\d{4}-\d{1,2}"#, options: .regularExpression) == nil {
            return a
        }
        return nil
    }

    init(_ c: TrustConfidence) {
        let measured = c.isMeasuredShape
        let at = c.thresholds ?? .engine
        // Sample or demo data scores evidence 0 and so the whole figure 0;
        // that is not a measurement — "not yet measurable" (B4 L1).
        let sample = c.dimensions?.evidence?.isSample ?? false
        let shownPct = sample ? nil : c.pct
        pct = shownPct
        if shownPct != nil {
            // The server's band is the tone, so an engine that moves its
            // cut-offs moves the colour without an app release.
            tone = Self.tone(band: c.band ?? c.effectiveBand)
        } else if measured {
            tone = .warn
        } else {
            tone = Self.tone(band: c.band)
        }

        if let pct = shownPct {
            lineLabel = "\(pct)% confidence"
        } else if sample {
            lineLabel = "Confidence not yet measurable"
        } else if measured {
            lineLabel = c.label.flatMap { $0.contains("%") ? nil : $0 } ?? "Confidence not yet measurable"
        } else if let label = c.label {
            lineLabel = label
        } else if let band = c.band {
            lineLabel = band.prefix(1).uppercased() + band.dropFirst() + " confidence"
        } else {
            lineLabel = "Confidence not yet measurable"
        }
        isRenderable = measured || c.band != nil || c.label != nil
        reason = sample ? (c.reason.flatMap { $0.range(of: "sample", options: .caseInsensitive) != nil ? $0 : nil }
                           ?? "Sample data \u{2014} not measurable until your own data is in") : c.reason
        caution = c.caution
        meterFraction = Self.fraction(shownPct)
        meaning = c.meaning ?? (measured ? Self.engineMeaning : nil)

        guard let dims = c.dimensions else {
            showsWhy = false
            rows = []
            footer = Self.footerBase
            return
        }
        showsWhy = true
        rows = [Self.evidenceRow(dims.evidence, at: at, sample: sample), Self.accuracyRow(dims.accuracy, at: at),
                Self.freshnessRow(dims.freshness, at: at)]
        footer = Self.footer(dims: dims, caps: c.caps, applied: c.capsApplied ?? [])
    }

    /// What holds the overall figure down, in plain words (B4 M7): the two
    /// ceilings confidence_engine.overall applies, from the payload's caps
    /// when sent, else the engine's.
    static func footer(dims: TrustConfidence.Dimensions, caps: TrustConfidence.Caps?,
                       applied: [String] = []) -> String {
        let ntr = caps?.noTrackRecord ?? 70
        let staleCap = caps?.stale ?? 49
        let staleBelow = caps?.staleBelow ?? 50
        var s = footerBase
        if dims.accuracy?.pct == nil { s += " Without a track record here it stays at \(ntr)% or below." }
        if applied.contains("record_unproven") {
            s += " Until its record here shows it beats doing nothing, it stays at \(caps?.recordUnproven ?? ntr)% or below."
        }
        if applied.contains("record_against") {
            s += " Its record here leans against it, which holds it at \(caps?.recordAgainst ?? 49)% or below."
        }
        if let f = dims.freshness?.pct, f < staleBelow {
            s += " Data under \(staleBelow)% fresh holds it at \(staleCap)% or below."
        }
        if applied.contains("freshness_unmeasured") {
            s += " Nothing dates the data under it, which holds it at \(caps?.freshnessUnmeasured ?? 49)% or below."
        }
        return s
    }

    private static func notMeasured(_ title: String) -> Row {
        Row(title: title, value: "\u{2014}", tone: .warn, basis: "Not measured", detail: nil, meterFraction: nil)
    }

    /// "Sample: 12" (of n_full when sent) — the evidence count on its own
    /// line, since several bases carry no count (B4 M7).
    static func sampleText(_ d: TrustConfidence.Dimension) -> String? {
        guard let n = d.n else { return nil }
        return "Sample: \(n)" + (d.nFull.map { " of the \($0) a full read needs" } ?? "")
    }

    static func evidenceRow(_ d: TrustConfidence.Dimension?, at: TrustConfidence.Thresholds = .engine,
                            sample: Bool = false) -> Row {
        let title = "Evidence strength"
        guard let d else { return notMeasured(title) }
        let p = sample ? nil : d.pct
        return Row(title: title, value: percentText(p), tone: tone(pct: p, at: at),
                   basis: d.basis ?? (p == nil ? "Not measured" : ""),
                   detail: sample ? nil : sampleText(d), meterFraction: fraction(p))
    }

    static func accuracyRow(_ d: TrustConfidence.Dimension?, at: TrustConfidence.Thresholds = .engine) -> Row {
        let title = "Historical accuracy"
        guard let d else { return notMeasured(title) }
        guard let pct = d.pct else {
            return Row(title: title, value: "\u{2014}", tone: .warn,
                       basis: d.basis ?? "Not enough history yet (\(d.n ?? 0) measured, needs 5)",
                       detail: nil, meterFraction: nil)
        }
        // The lift against doing nothing (group P): the basis is the lift
        // sentence ("improved 4 of 6 times vs 1 of 6 when not acted on"),
        // the % how likely this kind beats doing nothing here — not a rate
        // shrunk toward even, so no "pulled toward 50%" note.
        var parts: [String] = [d.beatsLabel ?? "\(pct)% likely to beat doing nothing"]
        if let low = d.low, let high = d.high { parts.append("improved-rate range \(low)\u{2013}\(high)%") }
        var detail = parts.joined(separator: " \u{00B7} ")
        if d.source == "cohort" {
            detail = "From other restaurants on Cavnar \u{00B7} " + detail
        }
        return Row(title: title, value: percentText(pct), tone: tone(pct: pct, at: at),
                   basis: d.basis ?? "", detail: detail, meterFraction: fraction(pct))
    }

    static func freshnessRow(_ d: TrustConfidence.Dimension?, at: TrustConfidence.Thresholds = .engine) -> Row {
        let title = "Data freshness"
        guard let d else { return notMeasured(title) }
        return Row(title: title, value: percentText(d.pct), tone: tone(pct: d.pct, at: at),
                   basis: d.basis ?? (d.pct == nil ? "Not measured" : ""),
                   detail: mdyDate(asOf: d.asOf, asOfISO: d.asOfISO).map { "as of " + $0 },
                   meterFraction: fraction(d.pct))
    }

    /// "72 percent confidence. Only 3 reviews in 90 days." — the % is always
    /// spoken, never carried by colour alone.
    var accessibilityLabel: String {
        let spoken = pct.map { "\($0) percent confidence" } ?? lineLabel
        guard let reason, !reason.isEmpty else { return spoken }
        return "\(spoken). \(reason)"
    }
}

// MARK: - What kind of claim this is

/// The small tag that says whether a line was measured, computed, forecast,
/// inferred — or written by the model (ai_guard.CLAIM_KINDS, K4
/// `model_written`). Unknown kinds show nothing rather than a guess.
/// A tone the server decided ("good" / "warn" / "bad" / "neutral"), read leniently — a
/// non-string is nil, so the client's own thresholds apply instead (I10:
/// one source for label and colour thresholds).
struct ServerTone: Decodable, Hashable, Sendable {
    let value: String?

    init(_ value: String?) { self.value = value }

    init(from decoder: Decoder) throws {
        let s = (try? decoder.singleValueContainer().decode(String.self))?
            .trimmingCharacters(in: .whitespaces).lowercased()
        switch s {
        case "good", "warn", "bad", "neutral": value = s
        case "warning": value = "warn"
        default: value = nil
        }
    }
}

/// An optional server sentence that must never fail the payload it rides
/// on (a calibration note on a schedule block): a string, else nil.
struct LenientText: Codable, Hashable, Sendable {
    let value: String?

    init(_ value: String?) { self.value = value }

    init(from decoder: Decoder) throws {
        let s = (try? decoder.singleValueContainer().decode(String.self))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        value = (s?.isEmpty ?? true) ? nil : s
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let value { try c.encode(value) } else { try c.encodeNil() }
    }
}

/// How demand forecasts have held up here (K8 `demand_accuracy`):
/// `{mean_error_pct, bias_pct | actual_vs_forecast_pct, inside_range_pct,
/// n_nights, n_ranged}`. Every field lenient; decoding never fails the
/// schedule payload it rides on.
///
/// `inside_range_pct` is a share of `n_ranged` — the nights that had a
/// range — not of `n_nights` (B6#10, B1 H7: "100% of 10 nights" was 1 of 1).
/// The signed figure is actual over forecast (positive: nights came in
/// above the forecast); `bias_pct` is its older name.
struct DemandAccuracy: Codable, Equatable, Sendable {
    var meanErrorPct: Double?
    var biasPct: Double?
    var insideRangePct: Double?
    var nNights: Int?
    var nRanged: Int?
    var actualVsForecastPct: Double?

    enum CodingKeys: String, CodingKey {
        case meanErrorPct = "mean_error_pct"
        case biasPct = "bias_pct"
        case insideRangePct = "inside_range_pct"
        case nNights = "n_nights"
        case nRanged = "n_ranged"
        case actualVsForecastPct = "actual_vs_forecast_pct"
    }

    init(meanErrorPct: Double? = nil, biasPct: Double? = nil, insideRangePct: Double? = nil, nNights: Int? = nil,
         nRanged: Int? = nil, actualVsForecastPct: Double? = nil) {
        self.meanErrorPct = meanErrorPct; self.biasPct = biasPct
        self.insideRangePct = insideRangePct; self.nNights = nNights
        self.nRanged = nRanged; self.actualVsForecastPct = actualVsForecastPct
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        meanErrorPct = try? c.decodeIfPresent(Double.self, forKey: .meanErrorPct)
        biasPct = try? c.decodeIfPresent(Double.self, forKey: .biasPct)
        insideRangePct = try? c.decodeIfPresent(Double.self, forKey: .insideRangePct)
        nNights = try? c.decodeIfPresent(Int.self, forKey: .nNights)
        nRanged = try? c.decodeIfPresent(Int.self, forKey: .nRanged)
        actualVsForecastPct = try? c.decodeIfPresent(Double.self, forKey: .actualVsForecastPct)
    }

    private static func nights(_ n: Int) -> String { "\(n) night\(n == 1 ? "" : "s")" }

    /// "Demand forecasts here: inside the range on 64% of the 14 nights that
    /// had one · 21 nights measured · 12% mean error · nights came in 6%
    /// above the forecast on average" — nil when there is nothing measured.
    var sentence: String? {
        var parts: [String] = []
        if let inside = insideRangePct, let nr = nRanged, nr > 0 {
            parts.append("inside the range on \(Int(inside.rounded()))% of the \(Self.nights(nr)) that had one")
        }
        if let n = nNights, n > 0 { parts.append(Self.nights(n) + " measured") }
        if let e = meanErrorPct { parts.append("\(Int(e.rounded()))% mean error") }
        if let v = actualVsForecastPct ?? biasPct, v.isFinite, abs(v) >= 1 {
            parts.append("nights came in \(Int(abs(v).rounded()))% \(v > 0 ? "above" : "below") the forecast on average")
        }
        // A count of nights alone measures nothing.
        guard meanErrorPct != nil || (insideRangePct != nil && (nRanged ?? 0) > 0) else { return nil }
        return "Demand forecasts here: " + parts.joined(separator: " \u{00B7} ")
    }
}

/// A forecast computed in Python, not written by the model (H8):
/// `{"kind": "review_rating_week" | "marketing_reach_week", "predicted": 4.3,
/// "computed": true}` on the reviews and marketing reads. Never throws: a
/// forecast is supporting detail, and an odd value must not fail the read
/// it rides on — it just draws nothing.
struct ComputedForecast: Codable, Equatable, Sendable {
    var kind: String?
    var predicted: Double?
    var computed: Bool?

    enum CodingKeys: String, CodingKey { case kind, predicted, computed }

    init(kind: String? = nil, predicted: Double? = nil, computed: Bool? = nil) {
        self.kind = kind; self.predicted = predicted; self.computed = computed
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        kind = try? c.decodeIfPresent(String.self, forKey: .kind)
        predicted = try? c.decodeIfPresent(Double.self, forKey: .predicted)
        computed = try? c.decodeIfPresent(Bool.self, forKey: .computed)
    }

    /// "Next week's rating, computed from the trend: 4.3★" /
    /// "Next week's reach per post, carried forward: 1,240" — nil for an
    /// unknown kind or no figure.
    var line: String? {
        guard let p = predicted, p.isFinite else { return nil }
        switch kind {
        case "review_rating_week":
            return "Next week\u{2019}s rating, computed from the trend: " + String(format: "%.1f", p) + "\u{2605}"
        case "marketing_reach_week":
            return "Next week\u{2019}s reach per post, carried forward from last week: \(Int(p.rounded()).formatted())"
        default:
            return nil
        }
    }
}

/// A payload's `claim_kinds` map ({"this_week": "measured", …}), read
/// leniently: a non-string value is skipped, never a failed payload.
struct ClaimKindMap: Decodable, Hashable, Sendable {
    let kinds: [String: String]

    init(_ kinds: [String: String] = [:]) { self.kinds = kinds }

    private struct AnyKey: CodingKey {
        let stringValue: String
        init?(stringValue: String) { self.stringValue = stringValue }
        var intValue: Int? { nil }
        init?(intValue: Int) { nil }
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: AnyKey.self) else { kinds = [:]; return }
        var out: [String: String] = [:]
        for key in c.allKeys {
            if let v = (try? c.decodeIfPresent(String.self, forKey: key)) ?? nil { out[key.stringValue] = v }
        }
        kinds = out
    }

    subscript(_ key: String) -> String? { kinds[key] }
}

enum ClaimKind {
    static func label(kind: String?, modelWritten: Bool? = nil) -> String? {
        if modelWritten == true { return "AI-written" }
        switch kind?.lowercased() {
        case "measured": return "Measured"
        case "computed": return "Computed"
        case "forecast": return "Forecast"
        case "inferred": return "Inferred"
        case "suggestion": return "Suggestion"
        case "estimate": return "Estimate"
        case "partial": return "Partial"
        default: return nil
        }
    }
}
