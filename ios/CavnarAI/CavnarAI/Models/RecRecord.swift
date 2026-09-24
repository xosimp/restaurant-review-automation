import Foundation

// The owner's recommendation record on the phone — what they followed, what
// it did, and the one-tap answers that feed it (Recommendation ROI audit,
// wave 2). Every shape here is a server contract from API_REFERENCE.md →
// "Outcomes and value" and "Recommendation record"; every field the server
// may leave out is optional, and the pure rules the screens follow (when a
// rate is quotable, when a check-in is due, when a total exists) live here
// so the tests pin the rule rather than one rendered fixture.

// MARK: - Why not (structured reasons)

/// The six reasons behind a "Not for us", in the owner's words. The code is
/// what `rec_ledger.REASON_CODES` accepts on POST /recs/event,
/// /home/dismiss and /ask-cavnar/action — any other code is a 400.
enum RecReason: String, CaseIterable, Identifiable, Sendable {
    case alreadyDoing = "already_doing"
    case doesntFit = "doesnt_fit"
    case tooCostly = "too_costly"
    case badTiming = "bad_timing"
    case dontTrustData = "dont_trust_data"
    case other = "other"

    var id: String { rawValue }
    var code: String { rawValue }

    var label: String {
        switch self {
        case .alreadyDoing:  return "Already doing this"
        case .doesntFit:     return "Doesn\u{2019}t fit us"
        case .tooCostly:     return "Too costly"
        case .badTiming:     return "Bad timing"
        case .dontTrustData: return "Don\u{2019}t trust the numbers"
        case .other:         return "Other"
        }
    }

    /// The owner wording for a stored code, or nil for none / an unknown one.
    static func label(for code: String?) -> String? {
        guard let code, let reason = RecReason(rawValue: code) else { return nil }
        return reason.label
    }
}

// MARK: - Tracker-start replies

/// `tracker` on a reply that started a before-and-after measurement.
struct RecTracker: Decodable, Equatable, Sendable {
    let id: Int?
    let metric: String?
    let label: String?
    let evaluateOn: String?
    let windowDays: Int?
    let module: String?
    /// "measuring labor % until 10/21/26" — already M/D/YY.
    let labelText: String?

    enum CodingKeys: String, CodingKey {
        case id, metric, label, module
        case evaluateOn = "evaluate_on"
        case windowDays = "window_days"
        case labelText = "label_text"
    }
}

/// `tracker_refused` — why nothing started (another measurement on the same
/// number, no metric, not measurable). `reason` is the owner sentence.
struct RecTrackerRefused: Decodable, Equatable, Sendable {
    let code: String?
    let reason: String?
    let inFlightUntil: String?

    enum CodingKeys: String, CodingKey {
        case code, reason
        case inFlightUntil = "in_flight_until"
    }
}

enum RecTrackerNote {
    /// "Measuring labor % until 10/21/26", or the refusal's own reason; nil
    /// when the reply carried neither.
    static func line(tracker: RecTracker?, refused: RecTrackerRefused?) -> String? {
        if let tracker {
            if let text = tracker.labelText?.trimmingCharacters(in: .whitespaces), !text.isEmpty {
                return text.prefix(1).uppercased() + text.dropFirst()
            }
            if let label = tracker.label, let on = tracker.evaluateOn {
                return "Measuring \(label.prefix(1).lowercased() + label.dropFirst()) until \(CavnarDate.mdy(on))"
            }
            return "Measuring from today"
        }
        if let reason = refused?.reason?.trimmingCharacters(in: .whitespaces), !reason.isEmpty {
            return reason
        }
        return nil
    }

    /// The tracker line to show UNDER the server's `message`, or nil when the
    /// message already says it (Done's message ends "Now measuring …", and an
    /// in-flight refusal's reason is folded into Track's) — never twice.
    static func extraLine(message: String?, tracker: RecTracker?, refused: RecTrackerRefused?) -> String? {
        guard let line = line(tracker: tracker, refused: refused) else { return nil }
        guard let message, !message.isEmpty else { return line }
        let m = message.lowercased()
        if m.contains(line.lowercased()) { return nil }
        if let text = tracker?.labelText?.lowercased(), !text.isEmpty, m.contains(text) { return nil }
        if let reason = refused?.reason?.lowercased(), !reason.isEmpty, m.contains(reason) { return nil }
        return line
    }
}

// MARK: - Outcomes (GET /outcomes)

/// One tracker row, with the rec-ROI fields. The result line, the
/// attribution sentence and every date-bearing owner string arrive written.
struct RecOutcome: Decodable, Identifiable, Equatable, Sendable {
    struct Concurrent: Decodable, Equatable, Sendable {
        let kind: String?
        let label: String?
        let date: String?
    }
    /// A partial reading while the tracker runs — labelled partial, never a result.
    struct Interim: Decodable, Equatable, Sendable {
        let value: Double?
        let delta: Double?
        let deltaPct: Double?
        let asOf: String?
        let daysIn: Int?
        let verdict: String?
        enum CodingKeys: String, CodingKey {
            case value, delta, verdict
            case deltaPct = "delta_pct"
            case asOf = "as_of"
            case daysIn = "days_in"
        }
    }
    struct OwnerCheckin: Decodable, Equatable, Sendable {
        let didIt: String?
        let conditionsChanged: Bool?
        let at: String?
        enum CodingKeys: String, CodingKey {
            case at
            case didIt = "did_it"
            case conditionsChanged = "conditions_changed"
        }
    }

    let id: Int
    let title: String?
    let source: String?
    let sourceKey: String?
    let metric: String?
    let metricLabel: String?
    let unit: String?
    let status: String?
    let verdict: String?
    let module: String?
    let startedOn: String?
    let evaluateOn: String?
    let baselineValue: Double?
    let afterValue: Double?
    let delta: Double?
    let deltaPct: Double?
    let baselineKind: String?
    let attribution: String?
    let attributionLabel: String?
    let concurrent: [Concurrent]
    let recheckOn: String?
    let recheckVerdict: String?
    let validated: Bool?
    let counts: Bool?
    let resultLine: String?
    let summary: String?
    let informational: Bool?
    let dollarsMonthly: Double?
    let ownerCheckin: OwnerCheckin?
    let interim: Interim?

    enum CodingKeys: String, CodingKey {
        case id, title, source, metric, unit, status, verdict, module, delta, attribution
        case concurrent, validated, counts, summary, informational, interim
        case sourceKey = "source_key"
        case metricLabel = "metric_label"
        case startedOn = "started_on"
        case evaluateOn = "evaluate_on"
        case baselineValue = "baseline_value"
        case afterValue = "after_value"
        case deltaPct = "delta_pct"
        case baselineKind = "baseline_kind"
        case attributionLabel = "attribution_label"
        case recheckOn = "recheck_on"
        case recheckVerdict = "recheck_verdict"
        case resultLine = "result_line"
        case dollarsMonthly = "dollars_monthly"
        case ownerCheckin = "owner_checkin"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        title = try? c.decodeIfPresent(String.self, forKey: .title)
        source = try? c.decodeIfPresent(String.self, forKey: .source)
        sourceKey = try? c.decodeIfPresent(String.self, forKey: .sourceKey)
        metric = try? c.decodeIfPresent(String.self, forKey: .metric)
        metricLabel = try? c.decodeIfPresent(String.self, forKey: .metricLabel)
        unit = try? c.decodeIfPresent(String.self, forKey: .unit)
        status = try? c.decodeIfPresent(String.self, forKey: .status)
        verdict = try? c.decodeIfPresent(String.self, forKey: .verdict)
        module = try? c.decodeIfPresent(String.self, forKey: .module)
        startedOn = try? c.decodeIfPresent(String.self, forKey: .startedOn)
        evaluateOn = try? c.decodeIfPresent(String.self, forKey: .evaluateOn)
        baselineValue = try? c.decodeIfPresent(Double.self, forKey: .baselineValue)
        afterValue = try? c.decodeIfPresent(Double.self, forKey: .afterValue)
        delta = try? c.decodeIfPresent(Double.self, forKey: .delta)
        deltaPct = try? c.decodeIfPresent(Double.self, forKey: .deltaPct)
        baselineKind = try? c.decodeIfPresent(String.self, forKey: .baselineKind)
        attribution = try? c.decodeIfPresent(String.self, forKey: .attribution)
        attributionLabel = try? c.decodeIfPresent(String.self, forKey: .attributionLabel)
        concurrent = (try? c.decodeIfPresent([Concurrent].self, forKey: .concurrent)) ?? []
        recheckOn = try? c.decodeIfPresent(String.self, forKey: .recheckOn)
        recheckVerdict = try? c.decodeIfPresent(String.self, forKey: .recheckVerdict)
        validated = try? c.decodeIfPresent(Bool.self, forKey: .validated)
        counts = try? c.decodeIfPresent(Bool.self, forKey: .counts)
        resultLine = try? c.decodeIfPresent(String.self, forKey: .resultLine)
        summary = try? c.decodeIfPresent(String.self, forKey: .summary)
        informational = try? c.decodeIfPresent(Bool.self, forKey: .informational)
        dollarsMonthly = try? c.decodeIfPresent(Double.self, forKey: .dollarsMonthly)
        ownerCheckin = try? c.decodeIfPresent(OwnerCheckin.self, forKey: .ownerCheckin)
        interim = try? c.decodeIfPresent(Interim.self, forKey: .interim)
    }

    var isTracking: Bool { status == "tracking" }
    var isEvaluated: Bool { status == "evaluated" }

    /// "31.2%", "$1,240", "4.4★", "6.5h" — a reading in the metric's own unit.
    static func reading(_ value: Double?, unit: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value == value.rounded() ? String(Int(value)) : String(format: "%.1f", value)
        switch unit {
        case "%": return "\(trimmed)%"
        case "$": return "$" + value.commaFormatted
        case "\u{2605}": return String(format: "%.1f", value) + "\u{2605}"
        case "h": return "\(trimmed)h"
        case let u?: return u.isEmpty ? trimmed : "\(trimmed) \(u)"
        default: return trimmed
        }
    }

    /// The interim line while measuring: "Partial reading after 9 days:
    /// labor % 30.1%, −1.1% so far" — labelled partial, never a result. Nil
    /// when there is no reading yet (first day, or unmeasurable so far).
    var interimLine: String? {
        guard isTracking, let i = interim, let value = Self.reading(i.value, unit: unit) else { return nil }
        let days = i.daysIn.map { " after \($0) day\($0 == 1 ? "" : "s")" } ?? ""
        let label = (metricLabel ?? metric ?? "").lowercased()
        var s = "Partial reading\(days): \(label.isEmpty ? "" : label + " ")\(value)"
        if let pct = i.deltaPct {
            s += ", \(pct > 0 ? "+" : pct < 0 ? "\u{2212}" : "\u{00B1}")\(String(format: "%.1f", abs(pct)))% so far"
        }
        return s
    }

    /// "Measuring labor % until 10/21/26" while it runs.
    var measuringLine: String? {
        guard isTracking, let on = evaluateOn else { return nil }
        let label = (metricLabel ?? metric ?? "the number").lowercased()
        return "Measuring \(label) until \(CavnarDate.mdy(on))"
    }

    /// The re-check in plain words: held / faded / reversed, or when it is due.
    var recheckLine: String? {
        switch recheckVerdict {
        case "held": return "Held at the re-check"
        case "faded": return "Faded at the re-check \u{2014} no longer counted"
        case "reversed": return "Reversed at the re-check \u{2014} no longer counted"
        case "unknown": return "The re-check couldn\u{2019}t be read"
        default:
            guard isEvaluated, let on = recheckOn else { return nil }
            return "Re-checked on \(CavnarDate.mdy(on))"
        }
    }

    /// "Also changed these weeks: a price change (9/3/26), Labor Day (9/1/26)".
    var otherChangesLine: String? {
        let bits = concurrent.compactMap { c -> String? in
            guard let label = c.label, !label.isEmpty else { return nil }
            return c.date.map { "\(label) (\(CavnarDate.mdy($0)))" } ?? label
        }
        guard !bits.isEmpty else { return nil }
        return "Also changed these weeks: " + bits.joined(separator: ", ")
    }
}

struct RecOutcomesResponse: Decodable {
    let ok: Bool
    let outcomes: [RecOutcome]
    let caveat: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        outcomes = (try? c.decodeIfPresent([RecOutcome].self, forKey: .outcomes)) ?? []
        caveat = try? c.decodeIfPresent(String.self, forKey: .caveat)
    }
    enum CodingKeys: String, CodingKey { case ok, outcomes, caveat }
}

// MARK: - The check-in (#21)

enum RecCheckIn {
    /// The three answers POST /recs/checkin accepts, in the order shown.
    static let answers: [(code: String, label: String)] = [("yes", "Yes"), ("partly", "Partly"), ("no", "No")]

    /// Source-key prefixes of trackers no recommendation episode stands
    /// behind — started by hand, from an Ask conversation, or by a campaign
    /// send — which /recs/checkin would answer with a 404.
    static let episodeLessPrefixes = ["manual:", "ask:", "campaign:"]

    /// The ledger key a check-in answers — the tracker's source key, when it
    /// is a recommendation's.
    static func key(for o: RecOutcome) -> String? {
        guard let key = o.sourceKey?.trimmingCharacters(in: .whitespaces), !key.isEmpty,
              !episodeLessPrefixes.contains(where: { key.hasPrefix($0) }) else { return nil }
        return key
    }

    /// A tracked result that has landed and the owner has not checked in on:
    /// evaluated with a clear verdict, not an alert-opened (informational)
    /// row, no owner_checkin yet, and a key the check-in can name.
    static func isDue(_ o: RecOutcome) -> Bool {
        o.isEvaluated
            && o.ownerCheckin == nil
            && o.informational != true
            && ["improved", "worsened", "no_clear_change"].contains(o.verdict ?? "")
            && key(for: o) != nil
    }
}

// MARK: - Summary (GET /recs/summary)

struct RecSummary: Decodable, Equatable {
    struct Module: Decodable, Equatable {
        let shown: Int
        let answered: Int
        let accepted: Int
        let completed: Int
        let implemented: Int
        let dismissed: Int
        let ignored: Int
        let n: Int
        let acceptRate: Double?
        let acceptRateLow: Double?
        let acceptRateHigh: Double?
        let enough: Bool

        enum CodingKeys: String, CodingKey {
            case shown, answered, accepted, completed, implemented, dismissed, ignored, n, enough
            case acceptRate = "accept_rate"
            case acceptRateLow = "accept_rate_low"
            case acceptRateHigh = "accept_rate_high"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            func int(_ k: CodingKeys) -> Int { (try? c.decodeIfPresent(Int.self, forKey: k)) ?? 0 }
            shown = int(.shown); answered = int(.answered); accepted = int(.accepted)
            completed = int(.completed); implemented = int(.implemented); dismissed = int(.dismissed)
            ignored = int(.ignored); n = int(.n)
            acceptRate = try? c.decodeIfPresent(Double.self, forKey: .acceptRate)
            acceptRateLow = try? c.decodeIfPresent(Double.self, forKey: .acceptRateLow)
            acceptRateHigh = try? c.decodeIfPresent(Double.self, forKey: .acceptRateHigh)
            enough = (try? c.decodeIfPresent(Bool.self, forKey: .enough)) ?? false
        }

        /// Accepted, done, or made the change.
        var followed: Int { accepted + completed + implemented }
    }

    struct Tag: Decodable, Equatable {
        let tag: String
        let label: String?
        let module: String?
        let measured: Int?
        let improved: Int?
        let successRate: Double?
        let enough: Bool?
        enum CodingKeys: String, CodingKey {
            case tag, label, module, measured, improved, enough
            case successRate = "success_rate"
        }
    }

    struct MostEffective: Decodable, Equatable {
        let tag: String?
        let label: String?
        let module: String?
        let successRate: Double?
        let measured: Int?
        enum CodingKeys: String, CodingKey {
            case tag, label, module, measured
            case successRate = "success_rate"
        }
    }

    let ok: Bool
    let days: Int?
    let since: String?
    let byModule: [String: Module]
    let byTag: [Tag]
    let mostEffective: MostEffective?
    let minSettled: Int?
    let minMeasured: Int?

    enum CodingKeys: String, CodingKey {
        case ok, days, since
        case byModule = "by_module"
        case byTag = "by_tag"
        case mostEffective = "most_effective"
        case minSettled = "min_settled"
        case minMeasured = "min_measured"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        days = try? c.decodeIfPresent(Int.self, forKey: .days)
        since = try? c.decodeIfPresent(String.self, forKey: .since)
        byModule = (try? c.decodeIfPresent([String: Module].self, forKey: .byModule)) ?? [:]
        byTag = (try? c.decodeIfPresent([Tag].self, forKey: .byTag)) ?? []
        mostEffective = try? c.decodeIfPresent(MostEffective.self, forKey: .mostEffective)
        minSettled = try? c.decodeIfPresent(Int.self, forKey: .minSettled)
        minMeasured = try? c.decodeIfPresent(Int.self, forKey: .minMeasured)
    }

    /// Modules with anything shown, most shown first.
    var modulesInOrder: [(key: String, stats: Module)] {
        byModule.filter { $0.value.shown > 0 }
            .sorted { $0.value.shown != $1.value.shown ? $0.value.shown > $1.value.shown : $0.key < $1.key }
            .map { (key: $0.key, stats: $0.value) }
    }
}

enum RecSummaryFormat {
    /// The module's name where an owner reads it.
    static func moduleLabel(_ key: String) -> String {
        switch key {
        case "reviews": return "Reviews"
        case "labor": return "Labor"
        case "schedule": return "Scheduling"
        case "food", "inventory": return "Food cost"
        case "marketing": return "Marketing"
        case "intel": return "Intel"
        case "guests": return "Guests"
        case "ops": return "Operations"
        case "home": return "Home"
        case "ask": return "Ask Cavnar"
        case "other": return "Other"
        default: return key.prefix(1).uppercased() + key.dropFirst()
        }
    }

    private static func pct(_ v: Double) -> String { "\(Int((v * 100).rounded()))%" }

    /// "62%" when there are enough answered and ignored to quote a rate;
    /// "Not enough yet" otherwise — never a rate from three answers.
    static func rate(_ m: RecSummary.Module) -> String {
        guard m.enough, let r = m.acceptRate else { return "Not enough yet" }
        return pct(r)
    }

    /// "48–74% likely range" — only beside a quoted rate.
    static func range(_ m: RecSummary.Module) -> String? {
        guard m.enough, m.acceptRate != nil, let lo = m.acceptRateLow, let hi = m.acceptRateHigh else { return nil }
        return "\(pct(lo))\u{2013}\(pct(hi)) likely range"
    }

    /// "12 shown · 7 followed · 2 said no · 3 ignored" — ignored always
    /// said, because it is in the denominator.
    static func counts(_ m: RecSummary.Module) -> String {
        var bits = ["\(m.shown) shown", "\(m.followed) followed"]
        if m.dismissed > 0 { bits.append("\(m.dismissed) said no") }
        bits.append("\(m.ignored) ignored")
        return bits.joined(separator: " \u{00B7} ")
    }

    /// Why a rate is withheld: "3 of 10 settled — a rate shows at 10".
    static func notEnoughDetail(_ m: RecSummary.Module, minimum: Int?) -> String? {
        guard !m.enough else { return nil }
        let floor = minimum ?? 10
        return "\(m.n) of \(floor) settled \u{2014} a rate shows at \(floor)"
    }

    /// "Weekend staffing — 4 of 5 measured changes improved".
    static func mostEffectiveLine(_ e: RecSummary.MostEffective) -> String? {
        guard let label = e.label ?? e.tag, let rate = e.successRate, let measured = e.measured, measured > 0 else { return nil }
        let improved = Int((rate * Double(measured)).rounded())
        let subject = label.prefix(1).uppercased() + label.dropFirst()
        return "\(subject) \u{2014} \(improved) of \(measured) measured change\(measured == 1 ? "" : "s") improved"
    }
}

// MARK: - Timeline (GET /recs/timeline)

struct RecTimelineItem: Decodable, Identifiable, Equatable {
    let key: String
    let title: String
    let module: String?
    let tags: [String]
    let firstShownAt: String?
    let surfaces: [String]
    let answer: String
    let answeredAt: String?
    let reasonCode: String?
    let reason: String?
    let implementedAt: String?
    let trackerId: Int?

    /// One key may have several episodes (shown again after it expired).
    var id: String { "\(key)|\(firstShownAt ?? "")" }

    enum CodingKeys: String, CodingKey {
        case key, title, module, tags, surfaces, answer, reason
        case firstShownAt = "first_shown_at"
        case answeredAt = "answered_at"
        case reasonCode = "reason_code"
        case implementedAt = "implemented_at"
        case trackerId = "tracker_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? key
        module = try? c.decodeIfPresent(String.self, forKey: .module)
        tags = (try? c.decodeIfPresent([String].self, forKey: .tags)) ?? []
        firstShownAt = try? c.decodeIfPresent(String.self, forKey: .firstShownAt)
        surfaces = (try? c.decodeIfPresent([String].self, forKey: .surfaces)) ?? []
        answer = (try? c.decodeIfPresent(String.self, forKey: .answer)) ?? "open"
        answeredAt = try? c.decodeIfPresent(String.self, forKey: .answeredAt)
        reasonCode = try? c.decodeIfPresent(String.self, forKey: .reasonCode)
        reason = try? c.decodeIfPresent(String.self, forKey: .reason)
        implementedAt = try? c.decodeIfPresent(String.self, forKey: .implementedAt)
        trackerId = try? c.decodeIfPresent(Int.self, forKey: .trackerId)
    }

    /// What the owner did with it, in their words.
    var answerLabel: String {
        switch answer {
        case "accepted": return "Tracked"
        case "completed": return "Done"
        case "implemented": return "Made the change"
        case "dismissed": return "Not for us"
        case "snoozed": return "Not today"
        case "expired": return "Went unanswered"
        case "superseded": return "Replaced by a newer one"
        default: return "Open"
        }
    }

    /// Taken (accepted, done, made the change) — the answers a result can follow.
    var wasTaken: Bool { ["accepted", "completed", "implemented"].contains(answer) }

    /// "Not for us · too costly — we priced it last spring".
    var reasonLine: String? {
        let coded = RecReason.label(for: reasonCode)
        let free = reason?.trimmingCharacters(in: .whitespacesAndNewlines)
        switch (coded, free?.isEmpty == false ? free : nil) {
        case let (c?, f?): return "\(c) \u{2014} \(f)"
        case let (c?, nil): return c
        case let (nil, f?): return f
        default: return nil
        }
    }
}

struct RecTimelinePage: Decodable {
    let ok: Bool
    let items: [RecTimelineItem]
    let nextBefore: String?

    enum CodingKeys: String, CodingKey {
        case ok, items
        case nextBefore = "next_before"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        items = (try? c.decodeIfPresent([RecTimelineItem].self, forKey: .items)) ?? []
        nextBefore = try? c.decodeIfPresent(String.self, forKey: .nextBefore)
    }
}

// MARK: - What worked for you (#28)

/// GET /recs/what-worked?days=90|180 — sentences built without a model from
/// the owner's own measured record. Rendered exactly as given; hidden until
/// the server says there is enough behind them.
struct WhatWorked: Decodable, Equatable {
    let ok: Bool
    let days: Int?
    let enough: Bool
    let sentences: [String]
    /// `facts.caveat` ("Measured before and after, not proven cause.") — the
    /// one fact the card shows; the rest of `facts` is for the web's detail.
    let caveat: String?

    enum CodingKeys: String, CodingKey { case ok, days, enough, sentences, facts }
    private enum FactKeys: String, CodingKey { case caveat }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        days = try? c.decodeIfPresent(Int.self, forKey: .days)
        enough = (try? c.decodeIfPresent(Bool.self, forKey: .enough)) ?? false
        sentences = ((try? c.decodeIfPresent([String].self, forKey: .sentences)) ?? [])
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        let facts = try? c.nestedContainer(keyedBy: FactKeys.self, forKey: .facts)
        caveat = (try? facts?.decodeIfPresent(String.self, forKey: .caveat)) ?? nil
    }

    /// Shown only when the server vouches for it and there is something to say.
    var isShown: Bool { ok && enough && !sentences.isEmpty }
}

// MARK: - The value lines (#1, #14, #34)

enum RecValueFormat {
    static func money(_ v: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        let body = f.string(from: NSNumber(value: abs(v).rounded())) ?? String(Int(abs(v).rounded()))
        return (v < 0 ? "\u{2212}$" : "$") + body
    }

    /// "Net of 1 change that got worse: $980/month" — only when something
    /// did get worse; the improvements line above stays the improvements.
    static func netLine(_ d: HomeFollowThroughViewModel.ValueSummary.Delivered) -> String? {
        guard let worse = d.worsened, (worse.count ?? 0) > 0 else { return nil }
        let n = worse.count ?? 0
        let net = d.netMonthly ?? ((d.monthly ?? 0) - (worse.monthly ?? 0))
        var s = "Net of \(n) change\(n == 1 ? "" : "s") that got worse: \(money(net))/month"
        if let wm = worse.monthly, wm > 0 { s += " (\(money(wm))/month worse)" }
        return s + "."
    }

    /// The server's own sentence, shown only when the net is below zero.
    static func netNote(_ d: HomeFollowThroughViewModel.ValueSummary.Delivered) -> String? {
        guard let net = d.netMonthly, net < 0, let note = d.netNote, !note.isEmpty else { return nil }
        return note
    }

    /// "$3,420 measured since 7/1/26, over 41 days" — a sum of measured
    /// days. Nil when `total` is null: nothing measured is not $0.
    static func cumulativeLine(_ c: HomeFollowThroughViewModel.ValueSummary.Cumulative?) -> String? {
        guard let c, let total = c.total else { return nil }
        var s = "\(money(total)) measured"
        if let since = c.since, !since.isEmpty { s += " since \(CavnarDate.mdy(since))" }
        if let days = c.days, days > 0 { s += ", over \(days) day\(days == 1 ? "" : "s") a change held" }
        s += total < 0 ? " \u{2014} more got worse than improved." : " \u{2014} net of what got worse."
        return s
    }

    /// "2 validated at re-check · 1 got worse · 1 faded" — the counts that
    /// qualify the dollars; nil when all are zero.
    static func countsLine(_ d: HomeFollowThroughViewModel.ValueSummary.Delivered) -> String? {
        var bits: [String] = []
        if let v = d.validated, v > 0 {
            var s = "\(v) validated at re-check"
            if let vm = d.validatedMonthly, vm > 0 { s += " (\(money(vm))/month)" }
            bits.append(s)
        }
        if let w = d.worsened?.count, w > 0 { bits.append("\(w) got worse") }
        if let f = d.faded, f > 0 { bits.append("\(f) faded") }
        return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
    }
}
