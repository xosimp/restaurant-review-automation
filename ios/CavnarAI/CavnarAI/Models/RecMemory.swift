import Foundation

// What Cavnar AI remembers about a recommendation on screen (memory round,
// 9/29/26 — M1 "silences", "who_answered", "quiet_kinds", "conflicts"; M3's
// trim guard; M4's kind holds). Every field is additive on the server, so
// every type here decodes leniently: an odd value is nil or skipped, never a
// Home, brief or report that fails to decode. The same shapes ride on Home's
// cards and Needs-attention items, the brief's lines, the one-thing hero and
// the nightly report's actions.

/// Reads one trimmed, non-empty string; nil for anything else.
private func lenientText<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> String? {
    guard let s = (try? c.decodeIfPresent(String.self, forKey: key)) ?? nil else { return nil }
    let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
    return t.isEmpty ? nil : t
}

/// The owner's own earlier answer to advice that is back on screen —
/// reopened on a material change, re-armed when its trigger came back, or
/// re-offered after its silence ran out (rec_ledger.previous_answers):
/// "You passed on this on 3/12/26 ($120/mo then) — the figure has at least
/// doubled since." `text` is the server's sentence, dates already M/D/YY.
struct RecPreviousAnswer: Codable, Hashable, Sendable {
    let text: String
    var answer: String? = nil
    var answeredOn: String? = nil
    var reasonLabel: String? = nil
    var reopened: String? = nil

    enum CodingKeys: String, CodingKey {
        case text, answer, reopened
        case answeredOn = "answered_on"
        case reasonLabel = "reason_label"
    }

    init(text: String, answer: String? = nil, answeredOn: String? = nil,
         reasonLabel: String? = nil, reopened: String? = nil) {
        self.text = text; self.answer = answer; self.answeredOn = answeredOn
        self.reasonLabel = reasonLabel; self.reopened = reopened
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let t = lenientText(c, .text) else {
            throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "no text")
        }
        text = t
        answer = lenientText(c, .answer)
        answeredOn = lenientText(c, .answeredOn)
        reasonLabel = lenientText(c, .reasonLabel)
        reopened = lenientText(c, .reopened)
    }
}

/// A manager's (or employee's) decline still in force on advice the owner
/// is shown — it silenced it for that login only (rec_ledger.
/// delegate_answers): "Dana passed on this: already doing it (9/28/26)".
/// Only an account holder is sent it.
struct RecDelegateAnswer: Codable, Hashable, Sendable {
    let text: String
    var by: String? = nil

    enum CodingKeys: String, CodingKey { case text, by }

    init(text: String, by: String? = nil) { self.text = text; self.by = by }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let t = lenientText(c, .text) else {
            throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "no text")
        }
        text = t
        by = lenientText(c, .by)
    }
}

/// Advice that pulls against other advice (lever_conflicts.apply): "Trim
/// Tuesday" beside "Fill Tuesday", a reprice on a dish guests call poor
/// value, a promotion of a dish whose ingredient is critically low. The
/// weaker card carries it; the owner settles it once (POST /recs/conflict
/// {conflict, prefer}) and the same conflict resolves that way from then on.
struct RecConflict: Codable, Hashable, Sendable, Identifiable {
    /// One way to settle it — keep one card's advice (its signature), or
    /// "hold" to hold the card back.
    struct Choice: Codable, Hashable, Sendable, Identifiable {
        let signature: String
        var key: String? = nil
        let label: String
        var id: String { signature }

        enum CodingKeys: String, CodingKey { case signature, key, label }

        init(signature: String, key: String? = nil, label: String) {
            self.signature = signature; self.key = key; self.label = label
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let s = lenientText(c, .signature) else {
                throw DecodingError.dataCorruptedError(forKey: .signature, in: c, debugDescription: "no signature")
            }
            signature = s
            key = lenientText(c, .key)
            label = lenientText(c, .label) ?? (s == "hold" ? "Hold it" : "Keep it")
        }
    }

    struct Route: Codable, Hashable, Sendable {
        var mobile: String? = nil
    }

    let id: String
    var rule: String? = nil
    /// The server's name for the kind of conflict ("Trim vs fill").
    var label: String? = nil
    /// The other card's title, when the other side is a card.
    var with: String? = nil
    var why: String? = nil
    let choose: [Choice]
    var route: Route? = nil

    enum CodingKeys: String, CodingKey { case id, rule, label, with, why, choose, route }

    init(id: String, rule: String? = nil, label: String? = nil, with: String? = nil, why: String? = nil,
         choose: [Choice], route: Route? = nil) {
        self.id = id; self.rule = rule; self.label = label; self.with = with; self.why = why
        self.choose = choose; self.route = route
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let i = lenientText(c, .id) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "no id")
        }
        id = i
        rule = lenientText(c, .rule)
        label = lenientText(c, .label)
        with = lenientText(c, .with)
        why = lenientText(c, .why)
        choose = ((try? c.decodeIfPresent(HomeLenientList<Choice>.self, forKey: .choose)) ?? nil)?.items ?? []
        route = (try? c.decodeIfPresent(Route.self, forKey: .route)) ?? nil
    }

    /// The route the choice posts to — the server's mobile route when it
    /// names one under /mobile/api, else the shared one.
    var mobilePath: String {
        if let m = route?.mobile, m.hasPrefix("/mobile/api/") { return m }
        return "/mobile/api/recs/conflict"
    }

    /// Only a conflict with two ways to settle it can be answered.
    var isAnswerable: Bool { choose.count >= 2 }
}

/// A kind Cavnar AI stopped suggesting because this restaurant's own
/// measured results say it did no better than doing nothing (M4 "thresholds",
/// rec_learning.hold_ask) — asked as a question: "Keep suggesting trim
/// day?". Done keeps it; Not for us leaves it stopped. The answers carry
/// the server's own words.
struct HomeKindHold: Codable, Hashable, Identifiable, Sendable {
    struct Answers: Codable, Hashable, Sendable {
        var completed: String? = nil
        var notForUs: String? = nil
        enum CodingKeys: String, CodingKey {
            case completed
            case notForUs = "not_for_us"
        }
        init(completed: String? = nil, notForUs: String? = nil) {
            self.completed = completed; self.notForUs = notForUs
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            completed = lenientText(c, .completed)
            notForUs = lenientText(c, .notForUs)
        }
    }

    let key: String
    var kind: String? = nil
    let title: String
    var why: String? = nil
    var measured: Int? = nil
    var improved: Int? = nil
    var worsened: Int? = nil
    var doNothingPct: Int? = nil
    var answers: Answers? = nil
    var recKey: String? = nil
    var answerable: Bool? = nil

    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, kind, title, why, measured, improved, worsened, answers, answerable
        case doNothingPct = "do_nothing_pct"
        case recKey = "rec_key"
    }

    init(key: String, title: String, why: String? = nil, measured: Int? = nil, improved: Int? = nil,
         worsened: Int? = nil, doNothingPct: Int? = nil, answers: Answers? = nil, recKey: String? = nil,
         answerable: Bool? = nil) {
        self.key = key; self.title = title; self.why = why; self.measured = measured
        self.improved = improved; self.worsened = worsened; self.doNothingPct = doNothingPct
        self.answers = answers; self.recKey = recKey; self.answerable = answerable
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let k = lenientText(c, .key), let t = lenientText(c, .title) else {
            throw DecodingError.dataCorruptedError(forKey: .key, in: c, debugDescription: "no key or title")
        }
        key = k
        title = t
        kind = lenientText(c, .kind)
        why = lenientText(c, .why)
        func int(_ key: CodingKeys) -> Int? {
            if let i = (try? c.decodeIfPresent(Int.self, forKey: key)) ?? nil { return i }
            if let d = (try? c.decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return Int(d.rounded()) }
            return nil
        }
        measured = int(.measured)
        improved = int(.improved)
        worsened = int(.worsened)
        doNothingPct = int(.doNothingPct)
        answers = (try? c.decodeIfPresent(Answers.self, forKey: .answers)) ?? nil
        recKey = lenientText(c, .recKey)
        answerable = (try? c.decodeIfPresent(Bool.self, forKey: .answerable)) ?? nil
    }

    /// The key the answer posts — rec_key, else the ask's own key.
    var answerKey: String { recKey ?? key }

    /// Answerable unless the server said otherwise (an unpresented ask).
    var showsAnswers: Bool { answerable != false }

    /// "Keep suggesting it" / "Stop suggesting it" — the server's words,
    /// with the same words as fallback.
    var keepLabel: String { answers?.completed ?? "Keep suggesting it" }
    var stopLabel: String { answers?.notForUs ?? "Stop suggesting it" }

    /// "4 of 7 measured results improved · 3 got worse · 44% by chance" —
    /// the record the hold rests on, every figure the server's.
    var recordLine: String? {
        var parts: [String] = []
        if let m = measured, m > 0 {
            parts.append("\(improved ?? 0) of \(m) measured result\(m == 1 ? "" : "s") improved")
            if let w = worsened, w > 0 { parts.append("\(w) got worse") }
        }
        if let d = doNothingPct { parts.append("\(d)% by doing nothing") }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }
}

/// The words a card carries about its own history that the server does not
/// write itself (RecMemoryNote draws them).
enum RecMemoryLines {
    /// Said on a card shown again as a re-test of a kind that had gone
    /// quiet (decisions.quiet_state — M1 "quiet_kinds"): the payload carries
    /// only `retest: true`.
    static let retestLine = "Back for a re-test \u{2014} you\u{2019}d let this kind go quiet, so Cavnar AI is checking once more."

    /// "Back for a re-test on 11/28/26" beside a quieter kind
    /// (`quieter[].review_on`, already M/D/YY) — nil without a date.
    static func reviewOn(_ date: String?) -> String? {
        guard let d = date?.trimmingCharacters(in: .whitespacesAndNewlines), !d.isEmpty else { return nil }
        return "back for a re-test on " + d
    }
}
