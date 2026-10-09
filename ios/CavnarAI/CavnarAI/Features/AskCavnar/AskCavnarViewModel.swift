import Foundation
import Observation

struct ChatMessage: Identifiable {
    let id = UUID()
    let text: String
    let isUser: Bool
    /// True when the model hit max_tokens — the answer stops mid-thought and
    /// must not be presented as a finished one (audit 5.1).
    var wasTruncated: Bool = false
    /// Actions the assistant wants to take and deliberately cannot take
    /// itself. Rendered as confirm cards under the answer; nothing happens
    /// until the owner taps one.
    var proposals: [AskProposal] = []
    /// What this answer rests on — which modules it consulted, how confident
    /// it is, and any figure in it that could not be traced back to data the
    /// model was handed. Nil on the owner's own turns and on older stored
    /// messages, which predate the backend sending it.
    var evidence: AskEvidence?
    /// The answer's server id — what "Was this useful?" rates (POST
    /// /ask-cavnar/feedback). Nil on the owner's turns and older servers.
    var messageId: Int? = nil
    /// The answer's concrete suggestions, each a keyed recommendation the
    /// owner can answer (Done / Not for us). Live answers only.
    var suggestions: [AskSuggestion] = []
    /// The owner's rating of this answer, once given (true = useful).
    var rating: Bool? = nil
    /// After a No: true once the optional "What was missing?" line was sent
    /// or skipped, so the field closes. Web asks the same (dashboard.html
    /// _appendAskCavnarFeedback).
    var noteSettled: Bool = false
    /// What this login's ratings now say about answer length, once the
    /// server derived it (owner_memory.derive_rating_preferences — memory
    /// round 9/29/26, M2): "Got it — shorter answers for you (Account →
    /// Memory)". Nil until a rating returns one.
    var preferenceNote: String? = nil
    /// Set once this message's typewriter reveal has actually played. The
    /// view model (not the view) owns this because the view's own @State is
    /// torn down every time the screen goes away — without a model-level
    /// flag, leaving and coming back replayed every answer's typing
    /// animation from scratch, every time.
    var hasRevealed: Bool = false
    /// The iPhone card (iOS readability round, 10/8/26): the answer's
    /// labelled lead, read server-side out of the validated text. Nil for
    /// an answer without one — it then renders as text, as before.
    var card: AskCard? = nil
    /// The text after the card's lead — what "Full analysis" holds. Nil
    /// without a card.
    var detail: String? = nil
    /// The depth contract the answer was written to ("brief", "standard",
    /// "executive"); an executive answer without a card shows its first
    /// paragraph and folds the rest.
    var depth: String? = nil
    /// The validation's own caveats (`validation.caveats`), shown in the
    /// warning disclosure when no figure, cause or name is listed.
    var caveats: [String] = []
    /// An answer without a card: the follow-up questions its "Follow-ups:"
    /// line carried, drawn as "Ask next" chips, never as a line of text
    /// (iOS re-audit H1). A card keeps its own (`card.followUps`).
    var followUps: [String] = []

    /// The backend records what the owner did with a proposal as a
    /// "[Confirmed: …]" / "[Dismissed: …]" user turn (so the model knows).
    /// In the transcript that's a status line, not something the owner
    /// typed — rendered as a small centered note instead of a bubble.
    var isStatusLine: Bool {
        isUser && (text.hasPrefix("[Confirmed:") || text.hasPrefix("[Dismissed:"))
    }

    /// "[Confirmed: Email Fresh Co]" → ("Confirmed", "Email Fresh Co").
    var statusParts: (verb: String, label: String)? {
        guard isStatusLine, let colon = text.firstIndex(of: ":") else { return nil }
        let verb = String(text[text.index(after: text.startIndex)..<colon])
        var label = String(text[text.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
        if label.hasSuffix("]") { label.removeLast() }
        return (verb, label)
    }
}

/// One of an answer's own suggestions — a list item that starts with an
/// imperative verb and carries no untraced figure (ask_cavnar
/// .extract_suggestions, no second model call), keyed and presented on
/// `ask` (rec-ROI #48). An answered one is left out server-side.
struct AskSuggestion: Decodable, Hashable, Identifiable, Sendable {
    let text: String
    let recKey: String
    let answerable: Bool?
    /// The suggestion's own K1 confidence (ask_cavnar.suggestion_confidence)
    /// — drawn as a compact meter beside it. Nil from an older server.
    var confidence: TrustConfidence? = nil
    var id: String { recKey }

    enum CodingKeys: String, CodingKey {
        case text, answerable, confidence
        case recKey = "rec_key"
    }

    init(text: String, recKey: String, answerable: Bool?, confidence: TrustConfidence? = nil) {
        self.text = text
        self.recKey = recKey
        self.answerable = answerable
        self.confidence = confidence
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)
        recKey = try c.decode(String.self, forKey: .recKey)
        answerable = (try? c.decodeIfPresent(Bool.self, forKey: .answerable)) ?? nil
        confidence = (try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence)) ?? nil
    }

    /// Done / Not for us apply (the server sends answerable: true).
    var showsAnswers: Bool { answerable != false && !recKey.isEmpty }
}

/// The iPhone card (iOS readability round, 10/8/26) — the answer's
/// labelled lead as the server read it out of the validated text
/// (ask_cavnar.answer_card): every word is one the answer said. Read
/// leniently: an odd field is nil; a card with no headline fails to decode
/// and is carried as no card (`AskLenientCard`), never a failed answer.
struct AskCard: Decodable, Hashable, Sendable {
    let headline: String
    var summary: String? = nil
    var cause: String? = nil
    /// The answer's check could not support the Cause line — shown as a
    /// hypothesis, never a finding.
    var causeFlagged: Bool = false
    var action: String? = nil
    /// The `ask_tip` key the Do first line was recorded under, when it is a
    /// suggestion — its Done / Pass row goes under it.
    var actionKey: String? = nil
    /// Conditional by construction ("If you …, about …"); the server leaves
    /// a promise, a sum or an untraced figure off.
    var outcome: String? = nil
    var followUps: [String] = []

    enum CodingKeys: String, CodingKey {
        case headline, summary, cause, action, outcome
        case causeFlagged = "cause_flagged"
        case actionKey = "action_key"
        case followUps = "follow_ups"
    }

    init(headline: String, summary: String? = nil, cause: String? = nil, causeFlagged: Bool = false,
         action: String? = nil, actionKey: String? = nil, outcome: String? = nil, followUps: [String] = []) {
        self.headline = headline
        self.summary = summary
        self.cause = cause
        self.causeFlagged = causeFlagged
        self.action = action
        self.actionKey = actionKey
        self.outcome = outcome
        self.followUps = followUps
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func text(_ k: CodingKeys) -> String? {
            let v = ((try? c.decodeIfPresent(String.self, forKey: k)) ?? nil)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return (v?.isEmpty ?? true) ? nil : v
        }
        guard let h = text(.headline) else {
            throw DecodingError.dataCorruptedError(forKey: .headline, in: c, debugDescription: "no headline")
        }
        headline = h
        summary = text(.summary)
        cause = text(.cause)
        causeFlagged = ((try? c.decodeIfPresent(Bool.self, forKey: .causeFlagged)) ?? nil) ?? false
        action = text(.action)
        actionKey = text(.actionKey)
        outcome = text(.outcome)
        let ups = ((try? c.decodeIfPresent([String].self, forKey: .followUps)) ?? nil) ?? []
        followUps = Array(ups.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }.prefix(3))
    }
}

/// A `card` field read so that a malformed one is no card, never an answer
/// that fails to decode.
struct AskLenientCard: Decodable, Hashable, Sendable {
    let card: AskCard?
    init(card: AskCard?) { self.card = card }
    init(from decoder: Decoder) throws {
        card = try? AskCard(from: decoder)
    }
}

/// `validation` on an answer — only its caveats are read here.
struct AskValidation: Decodable, Hashable, Sendable {
    var caveats: [String] = []

    enum CodingKeys: String, CodingKey { case caveats }

    init(caveats: [String] = []) { self.caveats = caveats }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        caveats = (((try? c.decodeIfPresent([String].self, forKey: .caveats)) ?? nil) ?? [])
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
    }
}

/// What a stored answer was shown with (models.get_ask_history's `meta`,
/// iOS readability round #90): its confidence as measured then, what it
/// read, what did not check out and its card — so a reopened chat draws it
/// the same, never re-measured.
struct AskStoredView: Decodable, Hashable, Sendable {
    var confidence: TrustConfidence? = nil
    var modules: [String] = []
    var unverifiedFigures: [String] = []
    var unsupportedCauses: [String] = []
    var unsupportedNames: [String] = []
    var declinedRepeats: [AskDeclinedRepeat] = []
    var caveats: [String] = []
    var card: AskCard? = nil

    enum CodingKeys: String, CodingKey {
        case caveats, card
        case confidence = "confidence_detail"
        case modules = "modules_consulted"
        case unverifiedFigures = "unverified_figures"
        case unsupportedCauses = "unsupported_causes"
        case unsupportedNames = "unsupported_names"
        case declinedRepeats = "declined_repeats"
    }

    init() {}

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        func strings(_ k: CodingKeys) -> [String] {
            ((try? c.decodeIfPresent([String].self, forKey: k)) ?? nil) ?? []
        }
        confidence = (try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence)) ?? nil
        modules = strings(.modules)
        unverifiedFigures = strings(.unverifiedFigures)
        unsupportedCauses = strings(.unsupportedCauses)
        unsupportedNames = strings(.unsupportedNames)
        caveats = strings(.caveats)
        declinedRepeats = ((try? c.decodeIfPresent(HomeLenientList<AskDeclinedRepeat>.self,
                                                   forKey: .declinedRepeats)) ?? nil)?.items ?? []
        card = ((try? c.decodeIfPresent(AskLenientCard.self, forKey: .card)) ?? nil)?.card
    }

    var evidence: AskEvidence {
        AskEvidence(modules: modules, confidence: AskMeta.pick(confidence, nil),
                    unverifiedFigures: unverifiedFigures, unsupportedCauses: unsupportedCauses,
                    unsupportedNames: unsupportedNames, declinedRepeats: declinedRepeats)
    }
}

/// The provenance of one answer.
///
/// Before this the API returned the answer as a bare markdown string, so the
/// app could not show which modules an answer came from or flag a number
/// that did not check out — however carefully the backend had worked both
/// out. `unverifiedFigures` is the important one: it means a currency or
/// percentage in the text does not appear in anything the model was given,
/// and the owner is about to act on it.
struct AskEvidence: Decodable, Hashable {
    var modules: [String] = []
    /// K5: the K1 object (a percentage computed from what the tools
    /// returned, with "Why?"). An older server's band string ("high") still
    /// decodes; nil when the server said nothing.
    var confidence: TrustConfidence? = nil
    var unverifiedFigures: [String] = []
    /// A cause, or a name, the answer states that nothing Cavnar read
    /// supports (ask_cavnar meta `unsupported_causes` / `unsupported_names`,
    /// NS1 H1). Shown inline like an untraced figure: the answer stays, the
    /// claim reads as a guess. Empty from a server that does not send them.
    var unsupportedCauses: [String] = []
    var unsupportedNames: [String] = []
    /// Suggestions in the answer the owner already said "not for us" to on
    /// some surface — kept, and caveated in place in the prose (memory
    /// round 9/29/26, M1 "relevance": decisions.annotate_declined →
    /// `declined_repeats`). Empty from a server that does not send them.
    var declinedRepeats: [AskDeclinedRepeat] = []

    /// Nothing worth drawing a strip for. A measured confidence is always
    /// worth its line; a legacy band word never is (iOS re-audit L4).
    var isEmpty: Bool {
        guard modules.isEmpty && unverifiedFigures.isEmpty && declinedRepeats.isEmpty
                && unsupportedCauses.isEmpty && unsupportedNames.isEmpty else { return false }
        return confidenceLabel == nil
    }

    /// The line's label — "72% confidence" — only for a measured
    /// percentage (iOS re-audit L4): an older server's band word ("Medium
    /// confidence") says how sure without saying how it was measured, so
    /// it draws nothing.
    var confidenceLabel: String? {
        guard let c = confidence, c.pct != nil else { return nil }
        let d = ConfidenceDisplay(c)
        return d.isRenderable ? d.lineLabel : nil
    }

    var warning: String? {
        guard !unverifiedFigures.isEmpty else { return nil }
        let list = unverifiedFigures.joined(separator: ", ")
        let noun = unverifiedFigures.count > 1 ? "those figures" : "that figure"
        return "Couldn’t verify \(list) against your data — treat \(noun) as unconfirmed."
    }

    /// "Unsupported cause — …", worded as the web's caveat.
    var causeWarning: String? {
        guard let first = unsupportedCauses.first(where: { !$0.isEmpty }) else { return nil }
        return "Unsupported cause \u{2014} Cavnar AI gave a reason here that nothing it read supports (\u{201C}\(String(first.prefix(120)))\u{201D}). Treat it as a guess, not a finding."
    }

    /// "Unverified name — …".
    var nameWarning: String? {
        let names = unsupportedNames.filter { !$0.isEmpty }
        guard !names.isEmpty else { return nil }
        return "Unverified name \u{2014} \(names.prefix(2).joined(separator: ", ")) doesn\u{2019}t appear in anything Cavnar AI read. Check before acting on it."
    }

    /// Every warning line, in the order the strip draws them.
    var warnings: [String] { [warning, causeWarning, nameWarning].compactMap { $0 } }

    /// The one compressed line an answer carries for what did not check out
    /// ("2 figures unverified · 1 reason is a guess"); the full caveats sit
    /// behind its Details. Nil when everything checked out.
    var caveatSummary: String? {
        var parts: [String] = []
        let figures = unverifiedFigures.filter { !$0.isEmpty }.count
        if figures > 0 { parts.append("\(figures) figure\(figures == 1 ? "" : "s") unverified") }
        let causes = unsupportedCauses.filter { !$0.isEmpty }.count
        if causes > 0 { parts.append("\(causes) reason\(causes == 1 ? " is a guess" : "s are guesses")") }
        let names = unsupportedNames.filter { !$0.isEmpty }.count
        if names > 0 { parts.append("\(names) name\(names == 1 ? "" : "s") unverified") }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }

    /// The shared amber caveats, one per kind — what the compressed line's
    /// Details opens to ("Hypothesis" for a cause).
    var caveatCards: [CavnarCaveat] {
        var out: [CavnarCaveat] = []
        if !unverifiedFigures.isEmpty { out.append(.unverifiedFigures(unverifiedFigures)) }
        if !unsupportedCauses.isEmpty { out.append(.unverifiedCauses(unsupportedCauses)) }
        if !unsupportedNames.isEmpty { out.append(.unverifiedNames(unsupportedNames)) }
        return out
    }

    /// "1 suggestion here is one you said not for us to on 8/12/26 —
    /// marked in the answer." Nil with none.
    var declinedLine: String? {
        let n = declinedRepeats.count
        guard n > 0 else { return nil }
        if n == 1, let on = declinedRepeats[0].declinedOn {
            return "1 suggestion here is one you passed on, on \(on) \u{2014} marked in the answer."
        }
        return "\(n) suggestion\(n == 1 ? " here is one" : "s here are ones") you passed on before \u{2014} each marked in the answer."
    }
}

/// One suggestion in an answer that repeats advice the owner declined
/// (`declined_repeats`: {text, signature, declined_on M/D/YY}).
struct AskDeclinedRepeat: Codable, Hashable, Sendable {
    let text: String
    var declinedOn: String? = nil

    enum CodingKeys: String, CodingKey {
        case text
        case declinedOn = "declined_on"
    }

    init(text: String, declinedOn: String? = nil) { self.text = text; self.declinedOn = declinedOn }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)
        let on = ((try? c.decodeIfPresent(String.self, forKey: .declinedOn)) ?? nil)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        declinedOn = (on?.isEmpty ?? true) ? nil : on
    }
}

/// K5 names the answer's metadata `meta`; today's server merges it into the
/// answer itself. Both places are read — the top-level one first.
struct AskMeta: Decodable, Hashable, Sendable {
    let confidence: TrustConfidence?
    /// `declined_repeats` when the server nests it under `meta`.
    var declinedRepeats: [AskDeclinedRepeat] = []

    enum CodingKeys: String, CodingKey {
        case confidence
        case confidenceDetail = "confidence_detail"
        case declinedRepeats = "declined_repeats"
    }

    /// Never throws: a `meta` that is not an object is no metadata, not an
    /// answer event that fails to decode.
    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { confidence = nil; return }
        confidence = (try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidenceDetail))
            ?? (try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence))
        declinedRepeats = ((try? c.decodeIfPresent(HomeLenientList<AskDeclinedRepeat>.self,
                                                   forKey: .declinedRepeats)) ?? nil)?.items ?? []
    }

    /// The top-level list, else the one nested in `meta`.
    static func declined(_ topLevel: HomeLenientList<AskDeclinedRepeat>?, _ meta: AskMeta?) -> [AskDeclinedRepeat] {
        let top = topLevel?.items ?? []
        return top.isEmpty ? (meta?.declinedRepeats ?? []) : top
    }

    static func pick(_ topLevel: TrustConfidence?, _ meta: AskMeta?) -> TrustConfidence? {
        for c in [topLevel, meta?.confidence] {
            if let c, ConfidenceDisplay(c).isRenderable { return c }
        }
        return nil
    }
}

/// A proposed action. `route` is the same authenticated endpoint the app's
/// own button uses, so confirming can never reach anything the user could
/// not already do themselves.
struct AskProposal: Decodable, Identifiable, Hashable {
    let action: String
    let summary: String
    let route: Route
    let body: [String: AnyCodableValue]?
    /// The ask_cavnar_actions row this card is. Answers are recorded against
    /// it, so confirming one supplier order never settles another with the
    /// same action name. Nil from an older backend.
    let proposalId: Int?
    /// What the owner is agreeing to beyond the one-line summary: the money,
    /// who it goes to, the words that would go out.
    let details: [Detail]?
    let preview: String?
    let atStake: Double?
    /// Every field the confirmed route will receive, labelled (NS5 C1): the
    /// card shows all of them and confirm posts only these keys. Nil from
    /// an older backend, when the whole body is posted as before.
    let fieldsShown: [ShownField]?

    var id: String { proposalId.map { "p\($0)" } ?? (action + summary) }

    struct Route: Decodable, Hashable {
        let mobile: String
        let method: String
        /// Where a job the route starts is polled to its end (a schedule
        /// build, a competitor refresh): `mobile` + the job id. Nil for a
        /// route that finishes in its own request, and from an older server.
        var status: Status? = nil

        struct Status: Decodable, Hashable {
            var web: String? = nil
            var mobile: String? = nil
        }
    }

    struct Detail: Decodable, Hashable {
        let label: String
        let value: String
    }

    struct ShownField: Decodable, Hashable {
        let key: String
        let label: String
        let value: String
    }

    enum CodingKeys: String, CodingKey {
        case action, summary, route, body, details, preview
        case proposalId = "proposal_id"
        case atStake = "at_stake"
        case fieldsShown = "fields_shown"
    }

    /// What confirm posts: only the fields the card showed.
    var postedBody: [String: AnyCodableValue] {
        let all = body ?? [:]
        guard let shown = fieldsShown else { return all }
        let keys = Set(shown.map(\.key))
        return all.filter { keys.contains($0.key) }
    }
}

/// One row in the chat history: a past conversation the owner can reopen
/// or delete.
struct AskConversation: Decodable, Identifiable, Hashable {
    let id: Int
    let title: String
    let preview: String
    let messageCount: Int
    let updatedAt: String
    let createdAt: String

    enum CodingKeys: String, CodingKey {
        case id, title, preview
        case messageCount = "message_count"
        case updatedAt = "updated_at"
        case createdAt = "created_at"
    }
}

/// Minimal JSON value box — proposal bodies are small and untyped
/// (a supplier email, a schedule id), and this avoids inventing a
/// separate Swift type per action.
///
/// Lists and objects are kept as they came (memory round 9/29/26): the Ask
/// card `set_staff_unavailable` posts `unavailable_days` as a list, and a
/// list read as null posted `unavailable_days: null` — the availability
/// route then saved the person as available every day, wiping the days
/// they had blocked. Every value round-trips now.
indirect enum AnyCodableValue: Decodable, Hashable, Encodable {
    case string(String), int(Int), double(Double), bool(Bool), null
    case array([AnyCodableValue])
    case object([String: AnyCodableValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Int.self) { self = .int(v) }
        else if let v = try? c.decode(Double.self) { self = .double(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([AnyCodableValue].self) { self = .array(v) }
        else if let v = try? c.decode([String: AnyCodableValue].self) { self = .object(v) }
        else { self = .null }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .int(let v):    try c.encode(v)
        case .double(let v): try c.encode(v)
        case .bool(let v):   try c.encode(v)
        case .null:          try c.encodeNil()
        case .array(let v):  try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
}

/// The publish gate's answer (409 `needs_ack`): `blockers` arrives as
/// strings or as `{key, text}` objects, `blocker_keys` beside them on some
/// routes — the web's `cavBlockers`. Pure, so the shapes are pinned by tests.
enum AskBlockers {
    struct Gate: Decodable {
        let error: String?
        let needsAck: Bool
        let texts: [String]
        /// The keys to acknowledge, one per text — nil when the server sent
        /// none, and the acknowledgement is then `true` (re-audit F2-9).
        let keys: [String]?

        enum CodingKeys: String, CodingKey {
            case error, blockers
            case needsAck = "needs_ack"
            case blockerKeys = "blocker_keys"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            error = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
            needsAck = ((try? c.decodeIfPresent(Bool.self, forKey: .needsAck)) ?? nil) ?? false
            let raw = (try? c.decodeIfPresent([AnyCodableValue].self, forKey: .blockers)) ?? nil
            texts = AskBlockers.texts(raw)
            let ownKeys = AskBlockers.keys(raw)
            let sent = ((try? c.decodeIfPresent([AnyCodableValue].self, forKey: .blockerKeys)) ?? nil)?
                .compactMap(AskBlockers.scalar) ?? []
            if let ownKeys, ownKeys.count == texts.count, !ownKeys.isEmpty {
                keys = ownKeys
            } else if !sent.isEmpty, sent.count == texts.count {
                keys = sent
            } else {
                keys = nil
            }
        }

        /// The value posted back as `acknowledge`: the keys shown, or true.
        var acknowledgement: AnyCodableValue {
            keys.map { .array($0.map { .string($0) }) } ?? .bool(true)
        }
    }

    static func texts(_ raw: [AnyCodableValue]?) -> [String] {
        (raw ?? []).compactMap { v -> String? in
            switch v {
            case .string(let s): return s.isEmpty ? nil : s
            case .object(let o):
                if case .string(let s)? = o["text"], !s.isEmpty { return s }
                return nil
            default: return nil
            }
        }
    }

    /// Each object blocker's own key; nil when any blocker carries none.
    static func keys(_ raw: [AnyCodableValue]?) -> [String]? {
        var out: [String] = []
        for v in raw ?? [] {
            guard case .object(let o) = v, let k = o["key"].flatMap(scalar) else { return nil }
            out.append(k)
        }
        return out.isEmpty ? nil : out
    }

    static func scalar(_ v: AnyCodableValue) -> String? {
        switch v {
        case .string(let s): return s
        case .int(let i): return String(i)
        case .double(let d): return String(d)
        default: return nil
        }
    }

    /// The card's sentence for a gate it may not pass itself — acknowledging
    /// is the schedule's own step, never the confirm card's (the web's
    /// `_runAskCavnarProposal`).
    static func cardLine(error: String?, blockers: [String]) -> String {
        let lead = error ?? "This week has things to look at first."
        guard !blockers.isEmpty else { return lead }
        return lead + " " + blockers.joined(separator: " \u{00B7} ") + " \u{2014} open the week to send it knowingly."
    }
}

/// Real multi-turn chat, persisted server-side as conversations. The
/// backend replays the current conversation's stored turns to the model
/// on every question, so a short follow-up like "yes" resolves against
/// what was actually being discussed — and the same chat is there again
/// after an app restart, in the history list, next to every earlier one.
@Observable
@MainActor
final class AskCavnarViewModel {
    /// Mirrors ask_cavnar.py's `_MAX_HISTORY_MESSAGES`. Anything older is
    /// discarded server-side anyway; sending it only cost upload time on the
    /// weak connections this app runs on (audit 5.4).
    static let maxHistoryMessages = 12
    /// Mirrors ask_cavnar.py's `_MAX_HISTORY_TURN_LENGTH`.
    static let maxHistoryTurnLength = 2400
    /// Mirrors ask_cavnar.py's `_MAX_QUESTION_LENGTH`. Enforced here so the
    /// user sees the limit rather than having the tail of their question
    /// silently cut server-side (audit 5.2).
    static let maxQuestionLength = 2000
    /// Mirrors models._ASK_TRANSCRIPT_KEEP — the most one chat holds.
    static let maxLoadedMessages = 200

    var messages: [ChatMessage] = [] {
        didSet {
            if messages.count > Self.maxLoadedMessages {
                messages.removeFirst(messages.count - Self.maxLoadedMessages)
            }
        }
    }

    var question = "" {
        didSet {
            if question.count > Self.maxQuestionLength {
                question = String(question.prefix(Self.maxQuestionLength))
            }
        }
    }

    var isLoading = false
    /// What the assistant is doing right now — "Reading your reviews" — shown
    /// while a multi-tool answer is in flight. nil outside a request, and
    /// while the stream connects, so it never flashes stale text left over
    /// from the previous question.
    var statusLabel: String? {
        didSet {
            // The reasoning trail: the label that was running is ticked as
            // the next one starts. Every entry is the server's own progress
            // event, in the order the tool loop ran it — never a script.
            if let old = oldValue, !old.isEmpty, old != statusLabel, statusLabel != nil {
                progressTrail.append(old)
            }
        }
    }
    /// Labels already completed this turn, oldest first. Cleared with
    /// statusLabel at the end of a request.
    var progressTrail: [String] = []
    /// The answer's opening as the server streams it, sentence by sentence
    /// (AI cost audit 10/7/26 #68). Every sentence has already passed the
    /// same validation as the whole answer, so nothing unchecked shows. A
    /// preview only: the "answer" event replaces it, and "sentence_reset"
    /// (a tool round, a retried call) clears it. Empty outside a request.
    var streamingPreview = ""
    /// The orb's motion for the current moment, from the stream's `state`
    /// field. `.connecting` from the instant a question is sent until the
    /// first progress event arrives, so the orb never sits still.
    var orbState: CavnarOrbState = .connecting
    /// Transient failure notice shown above the input bar. Deliberately not a
    /// ChatMessage: an error appended as an assistant turn ends up replayed to
    /// Claude in `history` as something it supposedly said (audit 2.5).
    var errorBanner: String?

    // MARK: Conversations

    /// The chat on screen. nil until the first answer of a brand-new chat
    /// comes back with its id, or while there are no chats at all.
    var conversationId: Int?
    /// True after "New chat" until the first question is answered — tells
    /// the backend to open a fresh conversation for that question instead
    /// of appending to the current one. Created server-side on the first
    /// question, so an abandoned New chat never leaves an empty row.
    private var wantsNewConversation = false
    var conversations: [AskConversation] = []
    var isLoadingConversations = false
    /// True while the transcript of a past chat is being fetched to reopen.
    var isOpeningConversation = false
    private var hasLoadedInitial = false

    var remainingCharacters: Int { Self.maxQuestionLength - question.count }

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    var canSubmit: Bool {
        !question.trimmingCharacters(in: .whitespaces).isEmpty && !isLoading && !isOpeningConversation
    }

    private struct HistoryTurn: Encodable {
        let role: String
        let content: String
    }

    private struct AskBody: Encodable {
        let question: String
        let history: [HistoryTurn]
        let conversation_id: Int?
        let new_conversation: Bool
        let screen: AskScreen?
    }

    private struct StreamBody: Encodable {
        let question: String
        let conversation_id: Int?
        let new_conversation: Bool
        let screen: AskScreen?
    }

    /// Where the next question was asked from (an "Ask about this" on a
    /// review, a recommendation, a staffing card). Consumed by the next
    /// submit only: a question typed afterwards stands on its own.
    var pendingScreen: AskScreen?

    private struct AskResponse: Decodable {
        let ok: Bool
        let answer: String?
        let error: String?
        let truncated: Bool?
        let proposals: [AskProposal]?
        let conversationId: Int?
        let modulesConsulted: [String]?
        let confidence: TrustConfidence?
        /// The K1 object when the server keeps `confidence` as the band
        /// word for older builds (confidence audit, group E).
        var confidenceDetail: TrustConfidence? = nil
        let meta: AskMeta?
        let unverifiedFigures: [String]?
        var unsupportedCauses: [String]? = nil
        var unsupportedNames: [String]? = nil
        var declinedRepeats: HomeLenientList<AskDeclinedRepeat>? = nil
        let messageId: Int?
        let suggestions: [AskSuggestion]?
        /// The iPhone card and the text after it (iOS readability round).
        var card: AskLenientCard? = nil
        var detail: String? = nil
        var depth: String? = nil
        var validation: AskValidation? = nil
        /// The follow-up questions lifted off the text (iOS re-audit H1).
        var followUps: [String]? = nil

        enum CodingKeys: String, CodingKey {
            case ok, answer, error, truncated, proposals, confidence, suggestions, meta
            case card, detail, depth, validation
            case followUps = "follow_ups"
            case declinedRepeats = "declined_repeats"
            case confidenceDetail = "confidence_detail"
            case conversationId = "conversation_id"
            case modulesConsulted = "modules_consulted"
            case unverifiedFigures = "unverified_figures"
            case unsupportedCauses = "unsupported_causes"
            case unsupportedNames = "unsupported_names"
            case messageId = "message_id"
        }

        var evidence: AskEvidence {
            AskEvidence(modules: modulesConsulted ?? [],
                        confidence: AskMeta.pick(confidenceDetail ?? confidence, meta),
                        unverifiedFigures: unverifiedFigures ?? [],
                        unsupportedCauses: unsupportedCauses ?? [],
                        unsupportedNames: unsupportedNames ?? [],
                        declinedRepeats: AskMeta.declined(declinedRepeats, meta))
        }
    }

    /// POST /ask-cavnar/action — the web's body key for key: `body` is the
    /// proposal's own body, logged with the answer (client_api's
    /// log_ask_action), which the phone never sent, so every answer given
    /// on iOS was filed with no record of what was confirmed (parity audit
    /// #24, the request-body parity test).
    struct ActionOutcomeBody: Encodable {
        let action: String
        let outcome: String
        let summary: String
        let body: [String: AnyCodableValue]?
        let conversation_id: Int?
        let proposal_id: Int?
        let reason: String?
        /// The one-tap why on "Not now" (RecReason) — same six codes as
        /// every other Not for us.
        var reason_code: String? = nil
    }

    private typealias PlainOK = APIClient.OKResponse

    /// A confirmed action's response. Most routes do the work inline and
    /// just answer ok; schedule generation hands back a job id and
    /// finishes on a background thread (see confirm()).
    struct JobOrOK: Decodable {
        let ok: Bool
        let error: String?
        let jobId: String?
        /// POST /goals from a teammate: the goal waits for the owner
        /// (memory round 9/29/26, M2 — goal.proposed).
        var proposed: Bool? = nil
        /// The server recorded the confirm on this same request (the
        /// X-Cavnar-Proposal header, command_center.settle_confirmed), so no
        /// second request is sent (re-audit F1-9, parity audit #4).
        var proposalSettled: Bool? = nil
        /// What the route flags beside its ok — time off approved over a
        /// published week names who is still on it (F2-5).
        var warning: String? = nil
        /// The publish gate: what stopped the send, by name.
        var needsAck: Bool? = nil
        var blockers: [String] = []

        enum CodingKeys: String, CodingKey {
            case ok, error, proposed, warning, blockers
            case jobId = "job_id"
            case proposalSettled = "proposal_settled"
            case needsAck = "needs_ack"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
            error = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
            // A job id is a string today; read a number as one too.
            jobId = ((try? c.decodeIfPresent(String.self, forKey: .jobId)) ?? nil)
                ?? ((try? c.decodeIfPresent(Int.self, forKey: .jobId)) ?? nil).map(String.init)
            proposed = (try? c.decodeIfPresent(Bool.self, forKey: .proposed)) ?? nil
            proposalSettled = (try? c.decodeIfPresent(Bool.self, forKey: .proposalSettled)) ?? nil
            warning = (try? c.decodeIfPresent(String.self, forKey: .warning)) ?? nil
            needsAck = (try? c.decodeIfPresent(Bool.self, forKey: .needsAck)) ?? nil
            blockers = AskBlockers.texts((try? c.decodeIfPresent([AnyCodableValue].self, forKey: .blockers)) ?? nil)
        }
    }

    /// What a confirmed card did beyond "Done", when the route says so —
    /// "Sent to the owner to confirm" for a teammate's goal. Nil otherwise.
    private(set) var lastConfirmNote: String?
    /// The route's own warning beside its ok, shown on the card under Done
    /// (the web's `d.warning`, parity audit #4). Nil otherwise.
    private(set) var lastConfirmWarning: String?

    /// The line a goal a teammate set earns: it waits for an account holder.
    static let proposedGoalNote = "Sent to the owner to confirm"

    private struct JobStatus: Decodable {
        let ok: Bool
        let status: String?
        let error: String?
    }

    private struct ConversationsResponse: Decodable {
        let ok: Bool
        let conversations: [AskConversation]?
    }

    private struct StoredMessage: Decodable {
        let id: Int?
        let role: String
        let content: String
        let proposals: [AskProposal]?
        /// What the answer was shown with (#90) and the text after its card.
        var meta: AskStoredView? = nil
        var detail: String? = nil
    }

    private struct ConversationResponse: Decodable {
        let ok: Bool
        let messages: [StoredMessage]?
    }

    // MARK: Chat history

    /// First appearance of the tab after a fresh launch or sign-in: start a
    /// new chat, not the most recent one.
    ///
    /// This used to reopen the latest conversation, fully revealed, on the
    /// theory that resuming where the owner left off saved them a tap. In
    /// practice it meant tapping the home row's Ask Cavnar button dropped
    /// them wherever they'd last scrolled inside an old chat — sometimes
    /// mid-conversation — instead of the fresh composer the tap implies.
    /// Old chats are one tap away in History; this only changes what
    /// greets a brand new session.
    func loadInitialIfNeeded() async {
        guard !hasLoadedInitial else { return }
        hasLoadedInitial = true
        await refreshConversations()
        await loadOpening()
    }

    /// What the screen says before the owner types anything.
    ///
    /// It used to be three hardcoded questions, identical to the web's and
    /// never changing — while the platform was already computing
    /// situation-aware ones from real signals and only the Home tab used
    /// them. No model call: this is the first thing an owner sees, so it has
    /// to be instant, and every line is measured rather than written.
    /// Re-fetched once it goes stale, not held for the life of the process.
    /// An owner who answered the review this was warning about and came back
    /// to the tab was otherwise told about it again.
    func loadOpening(force: Bool = false) async {
        if !force, let loaded = openingLoadedAt,
           Date().timeIntervalSince(loaded) < Self.openingTTL { return }
        let generation = SessionScope.generation
        if let response: AskOpening = try? await client.send(
            "/mobile/api/ask-cavnar/opening", hapticOnError: false), response.ok,
           generation == SessionScope.generation {
            opening = response
            openingLoadedAt = Date()
        }
    }

    var opening: AskOpening?
    private var openingLoadedAt: Date?
    /// The SessionScope generation the question in flight was asked under.
    @ObservationIgnored private var answerGeneration = 0
    private static let openingTTL: TimeInterval = 5 * 60

    func refreshConversations() async {
        isLoadingConversations = true
        defer { isLoadingConversations = false }
        let generation = SessionScope.generation
        if let response: ConversationsResponse = try? await client.send(
            "/mobile/api/ask-cavnar/conversations", hapticOnError: false),
           response.ok, generation == SessionScope.generation {
            conversations = response.conversations ?? []
        }
    }

    /// Reopens a past chat. Old proposals are deliberately NOT rendered as
    /// live confirm cards again — a supplier order the owner already sent
    /// last week must not come back with a working Confirm button. The
    /// "[Confirmed: …]" status lines in the transcript show what happened.
    func open(_ conversation: AskConversation) async {
        await open(conversationId: conversation.id)
    }

    /// The same, by id — a nav path `ask?conversation=<id>` carries only
    /// the id (F3-15).
    func open(conversationId: Int) async {
        guard !isLoading else { return }
        isOpeningConversation = true
        defer { isOpeningConversation = false }
        let generation = SessionScope.generation
        do {
            let response: ConversationResponse = try await client.send(
                "/mobile/api/ask-cavnar/conversations/\(conversationId)", hapticOnError: false)
            guard response.ok, generation == SessionScope.generation else { return }
            let stored = response.messages ?? []
            messages = stored.map { m in
                // A reopened answer can still be rated ("Was this useful?");
                // its suggestions are not re-offered, like its proposals.
                // It keeps its card, confidence and evidence as they were
                // measured when it was given (#90).
                let isUser = m.role == "user"
                let view = isUser ? nil : m.meta
                let evidence = view?.evidence
                let card = view?.card
                // The stored text keeps the contract's scaffolding (the card
                // is re-read from it); the phone never draws it (H1).
                let stripped = isUser ? (text: m.content, followUps: [String]())
                                      : AskAnswerText.scaffoldingStripped(m.content)
                return ChatMessage(text: stripped.text.isEmpty ? m.content : stripped.text, isUser: isUser,
                                   evidence: (evidence?.isEmpty == false) ? evidence : nil,
                                   messageId: isUser ? nil : m.id, hasRevealed: true,
                                   card: card, detail: card == nil ? nil : m.detail,
                                   caveats: view?.caveats ?? [],
                                   followUps: card == nil ? stripped.followUps : [])
            }
            self.conversationId = conversationId
            wantsNewConversation = false
            errorBanner = nil
        } catch {
            errorBanner = "Couldn't open that chat — check your connection and try again."
        }
    }

    /// A fresh conversation. Nothing is created until the first question
    /// is answered, so backing out costs nothing.
    func startNewChat() {
        guard !isLoading else { return }
        messages = []
        conversationId = nil
        wantsNewConversation = true
        errorBanner = nil
        statusLabel = nil
    }

    /// Called on a fresh sign-in (see RootView's onChange(of:
    /// sessionStore.isAuthenticated)). This view model is a single @State
    /// on RootView that outlives sign-out/sign-in within the same app
    /// process — the same reason hasShownHomeIntro and selectedTab need
    /// their own reset there — so without this, signing out of one chat and
    /// back in (same account or a different one on a shared device) landed
    /// on whatever conversation and scroll position were left over, and
    /// `hasLoadedInitial` being permanently true meant even the old
    /// resume-latest-chat behavior never got a chance to run again either.
    ///
    /// Also called on sign-out and on every location switch (RootView), and
    /// scoped by SessionScope.generation: the opening briefing (held for a
    /// 5-minute TTL), the chat, the history list and a half-typed question
    /// all belong to one user at one location. The opening used to survive,
    /// so the next account saw the last one's briefing; a switch kept the
    /// old location's chat, and its next question 404'd. Anything still in
    /// flight from before — an answer streaming, a history fetch — is
    /// dropped when it lands (`answerGeneration`, the guards above).
    func reset() {
        hasLoadedInitial = false
        conversations = []
        opening = nil
        openingLoadedAt = nil
        // Not startNewChat(): it waits out an answer in flight, and this
        // must clear whatever the previous scope left on screen now.
        messages = []
        conversationId = nil
        wantsNewConversation = true
        errorBanner = nil
        statusLabel = nil
        question = ""
        pendingScreen = nil
    }

    /// Permanent. If it was the chat on screen, the screen becomes a new
    /// chat — the deleted transcript never lingers.
    func delete(_ conversation: AskConversation) async -> Bool {
        let wasOpen = conversation.id == conversationId
        // Drop it from the list first so the swipe-to-delete row leaves
        // immediately; put it back if the server refuses.
        let previous = conversations
        conversations.removeAll { $0.id == conversation.id }
        do {
            let response: PlainOK = try await client.send(
                "/mobile/api/ask-cavnar/conversations/\(conversation.id)", method: .delete)
            guard response.ok else { conversations = previous; return false }
        } catch {
            conversations = previous
            return false
        }
        if wasOpen { startNewChat() }
        return true
    }

    // MARK: Proposals

    /// Fire a confirmed proposal. Runs the app's own endpoint, then writes
    /// the audit line — never the other way round, so a recorded
    /// "confirmed" always means it really ran.
    ///
    /// A false return always leaves the reason in `errorBanner`, and
    /// `lastConfirmMayHaveRun` says whether the action may have happened
    /// anyway. The bare catch here used to return false with no message,
    /// and the card offered Confirm again as if nothing had happened — after
    /// a timeout on a guest text blast or a supplier order that may already
    /// have gone out, and after a refusal (quiet hours, a send limit) whose
    /// reason the owner then never saw (CLIENT-19).
    func confirm(_ proposal: AskProposal) async -> Bool {
        lastConfirmMayHaveRun = false
        lastConfirmNote = nil
        lastConfirmWarning = nil
        do {
            // The action request names its proposal (and chat), so the
            // server records the confirm in the SAME request when the route
            // says ok — the web card's headers (re-audit F1-9, parity #4). A
            // lost second request used to leave it "never confirmed", open to
            // a second Confirm from Still open.
            let headers = Self.confirmHeaders(proposalId: proposal.proposalId, conversationId: conversationId)
            let response: JobOrOK
            if proposal.route.method == "GET" {
                response = try await client.sendWithHeaders(proposal.route.mobile, method: .get, headers: headers)
            } else {
                response = try await client.sendWithHeaders(proposal.route.mobile, method: .post,
                                                            body: proposal.postedBody, headers: headers)
            }
            guard response.ok else {
                errorBanner = response.needsAck == true
                    ? AskBlockers.cardLine(error: response.error, blockers: response.blockers)
                    : (response.error ?? "That didn't go through — nothing was sent.")
                return false
            }
            // A route that only STARTED a job (a schedule build, a competitor
            // refresh) answers at once with its id. Done means the job
            // finished: polled at the route's own status address — the
            // schedule poll used to run for any job, so "refresh competitors"
            // reported "the schedule didn't finish" (parity audit #4).
            if let jobId = response.jobId, let statusPath = Self.statusPath(for: proposal) {
                switch await pollJob(statusPath + Self.pathComponent(jobId)) {
                case .done:
                    break
                case .failed(let reason):
                    errorBanner = reason ?? "That didn't finish. Open the module to see where it stopped."
                    return false
                case .stillRunning:
                    lastConfirmMayHaveRun = true
                    errorBanner = "This is still running. Check back in a minute before asking again."
                    return false
                }
            }
            if response.proposed == true { lastConfirmNote = Self.proposedGoalNote }
            lastConfirmWarning = response.warning
            if response.proposalSettled == true {
                // Recorded already; only the transcript line is mirrored.
                appendOutcomeLine(proposal, outcome: "confirmed", reason: nil)
            } else {
                await record(proposal, outcome: "confirmed")
            }
            return true
        } catch is CancellationError {
            return false
        } catch let error as APIClient.APIError where error.status == nil && error.mayHaveReachedServer {
            // No answer came back, but the request may have arrived and run.
            lastConfirmMayHaveRun = true
            errorBanner = "We lost the connection before this finished, so it may already have gone through. "
                        + "Check before confirming again."
            return false
        } catch let error as APIClient.APIError {
            // The server said no (its own reason), or the request never left.
            // The publish gate names what stopped it; acknowledging is the
            // schedule's own step, never this card's.
            if let gate = error.decodeBody(AskBlockers.Gate.self), gate.needsAck {
                errorBanner = AskBlockers.cardLine(error: gate.error ?? error.message, blockers: gate.texts)
            } else {
                errorBanner = error.message
            }
            return false
        } catch {
            errorBanner = error.localizedDescription
            return false
        }
    }

    /// The headers a confirm carries: the proposal it settles and the chat
    /// it belongs to, each only when known.
    static func confirmHeaders(proposalId: Int?, conversationId: Int?) -> [String: String] {
        var h: [String: String] = [:]
        if let p = proposalId, p > 0 { h["X-Cavnar-Proposal"] = String(p) }
        if let c = conversationId, c > 0 { h["X-Cavnar-Conversation"] = String(c) }
        return h
    }

    /// Where a started job is polled: the route's own `status.mobile`; for
    /// an older server that sends none, the schedule build's address for
    /// the schedule tool only — never for any other job.
    static func statusPath(for proposal: AskProposal) -> String? {
        if let s = proposal.route.status?.mobile, !s.isEmpty { return s }
        if proposal.action == "generate_schedule" { return "/mobile/api/labor/schedule-status/" }
        return nil
    }

    private static func pathComponent(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? id
    }

    /// Set by confirm() when its request timed out or dropped mid-flight —
    /// the action may have run even though no answer arrived.
    private(set) var lastConfirmMayHaveRun = false

    enum JobOutcome: Equatable { case done, failed(String?), stillRunning }

    /// Polls a started job to its real conclusion: the web's budget, 40
    /// polls 1.5s apart. A failed poll is not a failed job — only a finished
    /// answer decides it.
    private func pollJob(_ path: String) async -> JobOutcome {
        for _ in 0..<40 {
            try? await Task.sleep(for: .seconds(1.5))
            if Task.isCancelled { return .stillRunning }
            guard let status: JobStatus = try? await client.send(path, hapticOnError: false) else { continue }
            if let outcome = Self.jobOutcome(ok: status.ok, status: status.status, error: status.error) {
                return outcome
            }
        }
        return .stillRunning
    }

    /// One poll's answer: nil while it is still pending (or said nothing).
    static func jobOutcome(ok: Bool, status: String?, error: String?) -> JobOutcome? {
        guard let status, status != "pending" else { return nil }
        return (ok && status != "error") ? .done : .failed(error)
    }

    /// "Not now", with the owner's one-tap reason — Ask reads it before
    /// proposing the same thing again. The reason's owner wording also rides
    /// as the free-text `reason`, so the transcript line the model reads
    /// says why ("[Dismissed: Email Fresh Co — Too costly]").
    func dismiss(_ proposal: AskProposal, reason: String? = nil, reasonCode: RecReason? = nil) async {
        await record(proposal, outcome: "dismissed", reason: reason ?? reasonCode?.label,
                     reasonCode: reasonCode?.code)
    }

    private func record(_ proposal: AskProposal, outcome: String, reason: String? = nil,
                        reasonCode: String? = nil) async {
        let why = reason?.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanReason = (why?.isEmpty ?? true) ? nil : String(why!.prefix(300))
        let _: PlainOK? = try? await client.send(
            "/mobile/api/ask-cavnar/action", method: .post,
            body: ActionOutcomeBody(action: proposal.action, outcome: outcome,
                                    summary: proposal.summary, body: proposal.body,
                                    conversation_id: conversationId,
                                    proposal_id: proposal.proposalId, reason: cleanReason,
                                    reason_code: outcome == "dismissed" ? reasonCode : nil))
        appendOutcomeLine(proposal, outcome: outcome, reason: cleanReason)
    }

    /// The status line the backend wrote — mirrored locally so the
    /// transcript on screen matches what a reopen would show.
    private func appendOutcomeLine(_ proposal: AskProposal, outcome: String, reason: String?) {
        let verb = outcome == "confirmed" ? "Confirmed" : "Dismissed"
        let tail = reason.map { " — \($0)" } ?? ""
        messages.append(ChatMessage(text: "[\(verb): \(proposal.summary)\(tail)]", isUser: true, hasRevealed: true))
    }

    struct FeedbackBody: Encodable, Equatable {
        let message_id: Int
        let helpful: Bool
        /// Omitted when nil (synthesized encodeIfPresent), as on web.
        var note: String? = nil
    }

    /// "Was this useful?" — POST /ask-cavnar/feedback {message_id, helpful,
    /// note?}. Rating the same answer again replaces the rating server-side,
    /// so a No and then its note are one rating, never two. The rating is
    /// shown once the server has it; a failure leaves the question up to be
    /// asked again.
    @discardableResult
    func rate(_ message: ChatMessage, helpful: Bool, note: String? = nil) async -> Bool {
        guard let messageId = message.messageId else { return false }
        let clean = (note ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let sentNote: String? = clean.isEmpty ? nil : String(clean.prefix(Self.feedbackNoteMax))
        let r: FeedbackResponse? = try? await client.send(
            "/mobile/api/ask-cavnar/feedback", method: .post,
            body: FeedbackBody(message_id: messageId, helpful: helpful, note: sentNote),
            hapticOnError: false, retryTransient: false)
        guard r?.ok == true else { return false }
        if let idx = messages.firstIndex(where: { $0.id == message.id }) {
            messages[idx].rating = helpful
            if sentNote != nil { messages[idx].noteSettled = true }
            // Said once a chat: the preference stands until the ratings
            // change it, and repeating it under every answer is noise.
            if let note = Self.preferenceNote(r?.preference?.preference), note != lastPreferenceNote {
                messages[idx].preferenceNote = note
                lastPreferenceNote = note
            }
        }
        Haptic.success()
        return true
    }

    /// The web field's maxlength for the "What was missing?" line.
    static let feedbackNoteMax = 500

    /// POST /ask-cavnar/feedback's answer: `preference` is what this
    /// login's own ratings now say about answer length ({preference:
    /// "short"|"full", fact}), or null.
    struct FeedbackResponse: Decodable {
        struct Preference: Decodable {
            let preference: String?
            let fact: String?
        }
        let ok: Bool
        let error: String?
        var preference: Preference? = nil

        enum CodingKeys: String, CodingKey { case ok, error, preference }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
            error = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
            preference = (try? c.decodeIfPresent(Preference.self, forKey: .preference)) ?? nil
        }
    }

    /// "Got it — shorter answers for you (Account → Memory)" — the one
    /// line a derived preference earns; nil for anything else.
    static func preferenceNote(_ preference: String?) -> String? {
        switch preference {
        case "short": return "Got it \u{2014} shorter answers for you (Account \u{2192} Memory)"
        case "full": return "Got it \u{2014} fuller answers for you (Account \u{2192} Memory)"
        default: return nil
        }
    }

    /// The last preference line said in this chat.
    @ObservationIgnored private var lastPreferenceNote: String?

    /// The owner closed the "What was missing?" field without writing
    /// anything: the No already stands, so nothing more is sent.
    func skipFeedbackNote(_ message: ChatMessage) {
        if let idx = messages.firstIndex(where: { $0.id == message.id }) {
            messages[idx].noteSettled = true
        }
    }

    /// Marks an answer as having already played its typewriter reveal, so
    /// TypewriterText renders it instantly next time instead of retyping it.
    func markRevealed(_ id: UUID) {
        if let idx = messages.firstIndex(where: { $0.id == id }) {
            messages[idx].hasRevealed = true
        }
    }

    // MARK: Asking

    func submit() async {
        guard canSubmit else { return }
        let asked = question
        let screen = pendingScreen
        pendingScreen = nil
        errorBanner = nil
        // Captured from `messages` BEFORE appending the new question, so
        // this is exactly the prior back-and-forth — the backend appends
        // `question` itself as the final turn, matching ask_cavnar.py's
        // own `history` contract (prior turns only, not including the new
        // question). Only the plain-request fallback sends it; the stream
        // route replays the stored conversation instead.
        let history = messages
            .filter { !$0.isStatusLine }
            .suffix(Self.maxHistoryMessages)
            // Mirrors ask_cavnar._MAX_HISTORY_TURN_LENGTH. Raised with it when
            // executive answers got longer: truncating the assistant's own
            // previous turn meant a follow-up like "do the second one"
            // resolved against an answer whose second option had been cut off.
            .map { HistoryTurn(role: $0.isUser ? "user" : "assistant",
                               content: String($0.text.prefix(Self.maxHistoryTurnLength))) }
        messages.append(ChatMessage(text: asked, isUser: true))
        question = ""
        answerGeneration = SessionScope.generation
        isLoading = true
        statusLabel = nil
        orbState = .connecting
        streamingPreview = ""
        defer { isLoading = false; statusLabel = nil; progressTrail = []; orbState = .connecting; streamingPreview = "" }

        do {
            try await streamAnswer(for: asked, screen: screen)
        } catch where answerGeneration != SessionScope.generation {
            // Signed out or switched location mid-answer: this turn belongs
            // to a chat that is no longer on screen. Nothing to roll back.
        } catch is CancellationError {
            // The screen went away mid-request — roll the turn back silently.
            if messages.last?.isUser == true { messages.removeLast() }
            question = asked
        } catch is StreamCutAfterProgress {
            // Tools already ran server-side before the stream died. Asking
            // again through the plain route re-billed the model and every
            // tool it called, and could repeat a write proposal (CLIENT-28).
            // The server may still finish and file the answer, so point the
            // owner at the chat history rather than re-asking on their behalf.
            if messages.last?.isUser == true { messages.removeLast() }
            question = asked
            errorBanner = "The connection dropped while Cavnar AI was working on this. "
                        + "Its answer may still land in your chat history — check there before asking again."
            Task { await refreshConversations() }
        } catch {
            // Streaming failed before any answer arrived (connection refused,
            // a proxy stripping the response, decode failure on the first
            // line) — fall back to the plain request rather than surface a
            // dead end. A failure partway through, after some progress
            // already rendered, is not retried here: the loop already ran
            // real tool calls server-side, and re-running it would risk a
            // second attempt at the same write-tool proposal.
            do {
                let response: AskResponse = try await client.send(
                    "/mobile/api/ask-cavnar", method: .post,
                    body: AskBody(question: asked, history: history,
                                  conversation_id: conversationId, new_conversation: wantsNewConversation,
                                  screen: screen)
                )
                if response.ok { adopt(conversationId: response.conversationId) }
                appendAnswer(from: response.ok ? (response.answer ?? "") : (response.error ?? "Something went wrong."),
                            truncated: response.truncated == true, proposals: response.proposals ?? [],
                            evidence: response.ok ? response.evidence : nil,
                            messageId: response.ok ? response.messageId : nil,
                            suggestions: response.ok ? (response.suggestions ?? []) : [],
                            card: response.ok ? response.card?.card : nil,
                            detail: response.detail, depth: response.depth,
                            caveats: response.validation?.caveats ?? [],
                            followUps: response.ok ? (response.followUps ?? []) : [])
            } catch where answerGeneration != SessionScope.generation {
                // The scope moved on while the fallback ran; see above.
            } catch is CancellationError {
                if messages.last?.isUser == true { messages.removeLast() }
                question = asked
            } catch {
                // Roll the turn back rather than leaving a dead end: the
                // question returns to the input box ready to resend (it used
                // to be cleared and lost, making "try again" harder to follow
                // than it sounds), and the failure never enters `history`
                // (audit 2.5).
                if messages.last?.isUser == true { messages.removeLast() }
                question = asked
                errorBanner = (error as? APIClient.APIError)?.message
                    ?? "Couldn't reach Cavnar AI — check your connection and try again."
            }
        }
    }

    /// Consumes the SSE stream: "progress" updates statusLabel as each tool
    /// runs, "sentence" grows the validated preview ("sentence_reset"
    /// clears it), "answer" appends the final message, "error" surfaces the
    /// server's own message. Any other type is ignored. Mirrors the web client's fetch/ReadableStream
    /// loop — same events, same fallback-on-failure shape.
    private func streamAnswer(for question: String, screen: AskScreen?) async throws {
        var gotAnswer = false
        // Any progress event means the server's loop is running tools on
        // this question; from then on a failure must not trigger a re-ask.
        var sawProgress = false
        do {
            try await consumeStream(for: question, screen: screen, gotAnswer: &gotAnswer, sawProgress: &sawProgress)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            if sawProgress && !gotAnswer { throw StreamCutAfterProgress() }
            throw error
        }
        if !gotAnswer {
            // The stream closed with no "answer"/"error" event at all — a
            // proxy that buffers/drops SSE, most likely. Before any progress,
            // the caller's plain-request fallback is safe; after it, it is
            // a second paid run of the same question.
            if sawProgress { throw StreamCutAfterProgress() }
            throw APIClient.APIError(message: "Stream ended without an answer.")
        }
    }

    /// The stream failed after the server had started work on the question.
    struct StreamCutAfterProgress: Error {}

    private func consumeStream(for question: String, screen: AskScreen?, gotAnswer: inout Bool, sawProgress: inout Bool) async throws {
        for try await event in await client.stream(
            "/mobile/api/ask-cavnar/stream",
            body: StreamBody(question: question, conversation_id: conversationId,
                             new_conversation: wantsNewConversation, screen: screen)) {
            // A stream from before a sign-out or a location switch: its
            // events belong to a chat no longer on screen.
            guard answerGeneration == SessionScope.generation else { continue }
            switch event.type {
            case "progress":
                sawProgress = true
                statusLabel = event.label
                if let raw = event.state, let mapped = CavnarOrbState(rawValue: raw) {
                    orbState = mapped
                }
            case "sentence":
                // Validated server-side before it was sent; shown in the
                // in-flight bubble until the answer replaces it.
                sawProgress = true
                streamingPreview += event.text ?? ""
            case "sentence_reset":
                streamingPreview = ""
            case "answer":
                gotAnswer = true
                adopt(conversationId: event.conversationId)
                // Already on screen as the preview: the answer lands without
                // typing out again, so the swap reads as the same text
                // settling (and correcting itself if the final pass changed it).
                let previewed = !streamingPreview.isEmpty
                streamingPreview = ""
                appendAnswer(from: event.answer ?? "", truncated: event.truncated == true,
                            proposals: event.proposals ?? [], evidence: event.evidence,
                            messageId: event.messageId, suggestions: event.suggestions ?? [],
                            revealed: previewed, card: event.card?.card, detail: event.detail,
                            depth: event.depth, caveats: event.validation?.caveats ?? [],
                            followUps: event.followUps ?? [])
            case "error":
                gotAnswer = true
                streamingPreview = ""
                appendAnswer(from: event.error ?? "Something went wrong.", truncated: false,
                            proposals: [], evidence: nil)
            default:
                break
            }
        }
    }

    /// An answered question settles which chat we're in: a New chat now
    /// has an id, and the history list needs the new/updated row.
    private func adopt(conversationId id: Int?) {
        guard answerGeneration == SessionScope.generation else { return }
        if let id { conversationId = id }
        wantsNewConversation = false
        Task { await refreshConversations() }
    }

    private func appendAnswer(from raw: String, truncated: Bool, proposals: [AskProposal],
                              evidence: AskEvidence?, messageId: Int? = nil,
                              suggestions: [AskSuggestion] = [], revealed: Bool = false,
                              card: AskCard? = nil, detail: String? = nil, depth: String? = nil,
                              caveats: [String] = [], followUps: [String] = []) {
        guard answerGeneration == SessionScope.generation else { return }
        // Never a "---" rule or a "Follow-ups:" line as text (H1): an older
        // server still sends them; its follow-ups become the chips.
        let stripped = AskAnswerText.scaffoldingStripped(raw)
        let cleaned = stripped.text
        let display = cleaned.isEmpty
            ? "I didn't get an answer back that time — mind asking again?"
            : cleaned
        // An empty strip is carried as nil so the view has one thing to check
        // rather than reaching into the struct to decide whether to draw.
        let ev = (evidence?.isEmpty == false) ? evidence : nil
        // A card lands whole — it has no words to type out; its detail sits
        // behind "Full analysis", already revealed when opened.
        let shownCard = cleaned.isEmpty ? nil : card
        messages.append(ChatMessage(text: display, isUser: false, wasTruncated: truncated,
                                    proposals: proposals, evidence: ev, messageId: messageId,
                                    suggestions: suggestions.filter(\.showsAnswers),
                                    hasRevealed: revealed || shownCard != nil,
                                    card: shownCard, detail: shownCard == nil ? nil : detail,
                                    depth: depth, caveats: caveats,
                                    followUps: shownCard == nil
                                        ? Array((followUps.isEmpty ? stripped.followUps : followUps).prefix(3))
                                        : []))
    }

    /// A follow-up chip under an answer (#91): asks it as if typed. Ignored
    /// while an answer is in flight.
    func askFollowUp(_ text: String) async {
        guard !isLoading, !isOpeningConversation else { return }
        question = text
        await submit()
    }
}


/// The assistant's opening: what needs the owner today, and the questions
/// worth asking given the state of the business right now.
struct AskOpening: Codable, Equatable {
    struct Item: Codable, Equatable, Identifiable {
        let severity: String?
        let title: String?
        let detail: String?
        let module: String?
        var id: String { (title ?? "") + (detail ?? "") }
    }
    let ok: Bool
    let headline: String?
    let briefing: [Item]?
    let suggestions: [String]?
    let restaurant: String?
    let sinceLabel: String?

    enum CodingKeys: String, CodingKey {
        case ok, headline, briefing, suggestions, restaurant
        case sinceLabel = "since_label"
    }
}
