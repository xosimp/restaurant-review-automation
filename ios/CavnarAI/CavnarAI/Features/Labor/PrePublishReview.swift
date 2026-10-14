import Foundation

// The pre-publish review (schedule audit 10/10/26 TRUST-27, RM-6): ONE
// server payload — GET /mobile/api/labor/publish-review?schedule_id= (the
// twin of the web's /api/labor/publish-review) — drives the web's "Before
// it goes out" and the phone's Send sheet, so the two can never disagree.
// The shape is API_REFERENCE.md's "The pre-publish review" (I1, built to
// SHARED contract #6): {ok, schedule_id, week_start, week_end, version,
// verdict {tone, sentence}, must_fix [{key, text, day, fixes, override_allowed,
// tier}], decide [{key, text, options [{label, scope, action, payload}]}],
// numbers {labor_pct {value, low, high, basis, track_record}, cost,
// overtime_hours, forecast_sales {value, basis}, savings {value, basis,
// projected}}, changed {vs_last_week, vs_usual, vs_last_draft,
// cavnar_changes}, fragile [{text, action}], why {assignments [{text}],
// left_off [{name, reason}]}, sure {kept_share, kept_trend, predictor_hit,
// predictor_base, completeness}, send {reach {app, text, email,
// portal_only}, label}}. Every part decodes leniently: one odd field is
// that field missing, never a sheet that cannot open.

/// A server value read as words: a string, a number's text, or an object's
/// `text` / `label` / `sentence`. Nil when none of those.
private func reviewWords(_ v: LooseValue?) -> String? {
    switch v {
    case .string(let s)?:
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    case .number(let n)?:
        return n == n.rounded() ? String(Int(n)) : String(n)
    case .object(let o)?:
        return reviewWords(o["text"]) ?? reviewWords(o["label"]) ?? reviewWords(o["sentence"])
    default:
        return nil
    }
}

/// A server value read as a number: a number, or numeric text.
private func reviewNumber(_ v: LooseValue?) -> Double? {
    switch v {
    case .number(let n)?: return n.isFinite ? n : nil
    case .string(let s)?: return Double(s.trimmingCharacters(in: .whitespaces))
    default: return nil
    }
}

struct PublishReview: Decodable, Equatable {

    /// One tap the review offers — a fix, a decision's answer, a fragile
    /// spot's remedy. `action` names what it does; `payload` is what the
    /// server needs back; `scope` ("week" | "every") is how long a decision
    /// holds, stated on the button.
    struct Action: Decodable, Equatable, Identifiable {
        var label: String
        var action: String
        var payload: [String: LooseValue] = [:]
        var scope: String? = nil

        var id: String { "\(action)|\(label)|\(scope ?? "")" }

        enum CodingKeys: String, CodingKey { case label, action, payload, scope }

        init(label: String, action: String, payload: [String: LooseValue] = [:], scope: String? = nil) {
            self.label = label; self.action = action; self.payload = payload; self.scope = scope
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            label = c.sfText(.label) ?? ""
            action = c.sfText(.action) ?? ""
            if case .object(let o)? = (try? c.decodeIfPresent(LooseValue.self, forKey: .payload)) ?? nil {
                payload = o
            }
            scope = c.sfText(.scope)
            if label.isEmpty && action.isEmpty {
                throw DecodingError.dataCorruptedError(forKey: .label, in: c, debugDescription: "An action names nothing")
            }
        }

        /// The button's words, with the scope said where the label leaves it
        /// out ("3 is fine · every Monday" is the label's own job; a bare
        /// "3 is fine" with scope "every" gains "· from now on").
        var buttonLabel: String {
            let text = label.isEmpty ? action.replacingOccurrences(of: "_", with: " ").capitalized : label
            guard let scope else { return text }
            let low = text.lowercased()
            switch scope {
            case "every" where !low.contains("every") && !low.contains("always"):
                return text + " \u{00B7} from now on"
            case "week" where !low.contains("week"):
                return text + " \u{00B7} this week"
            default:
                return text
            }
        }
    }

    /// The verdict at the top (TRUST-6): green "Ready to send", amber
    /// "Ready after 2 decisions", red "Not ready: …".
    struct Verdict: Decodable, Equatable {
        var tone: String = "amber"
        var sentence: String = ""
        var subline: String? = nil
        /// What does not hold the send but should be seen first — overtime,
        /// a soft rule broken (TRUST-3): the verdict names them.
        var worthALook: [Line] = []

        enum CodingKeys: String, CodingKey {
            case tone, sentence, subline, text
            case worthALook = "worth_a_look"
        }

        init(tone: String, sentence: String, subline: String? = nil) {
            self.tone = tone; self.sentence = sentence; self.subline = subline
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            tone = (c.sfText(.tone) ?? "amber").lowercased()
            sentence = c.sfText(.sentence) ?? c.sfText(.text) ?? ""
            subline = c.sfText(.subline)
            worthALook = c.setupList(Line.self, .worthALook)
        }
    }

    /// Something that must be fixed — or overridden, on its own, with what
    /// the gate asks — before the week goes out. Its colour is its TIER
    /// (schedule re-audit 10/11/26 TRUST-24): everything in the must-fix
    /// tier is red, as on the web; a decision is amber and lives in
    /// `decide`. The gate's old `hard` flag meant "never demoted", not the
    /// tier, and is no longer read.
    struct MustFix: Decodable, Equatable, Identifiable {
        var key: String
        var text: String
        var day: String? = nil
        var fixes: [Action] = []
        var overrideAllowed = true
        var tier: String? = nil
        /// Red: the must-fix tier (every item the review lists here).
        var hard = true
        /// The rule's class (law / manager / person / coverage / policy /
        /// check) and whether the law stands behind it (TRUST-4, TRUST-8):
        /// a law an edit can fix is never sent past.
        var cls: String? = nil
        var law = false
        var kind: String? = nil
        /// What the override's field asks (`override.prompt`). Nil: the
        /// plain question.
        var overridePrompt: String? = nil
        /// What an override needs (`override.needs`): "reason",
        /// "covered_by" (a manager gap: a manager or owner on the roster
        /// who can work that day, TRUST-5) or "legal_ack" (the posting
        /// law: the owner's acknowledgement and a reason, TRUST-8).
        var overrideNeeds: String? = nil
        /// Why it cannot be sent past (`override.why`), when it can't.
        var overrideWhy: String? = nil
        /// The law's own sentence the acknowledgement ticks.
        var legalText: String? = nil
        /// A reason is at least this many characters and words (TRUST-4:
        /// "x" sent a week past the gate).
        var minReasonChars = 8
        var minReasonWords = 2
        /// A manager gap is sent past only by naming who runs the floor
        /// (ARCH-30); `candidates` are the managers and owners on the roster
        /// who can work that day — never a typed name (TRUST-5).
        var needsCoveredBy = false
        var candidates: [String] = []
        /// "You sent past this in 3 of 4 weeks" (TRUST-28), when the server
        /// says so.
        var overriddenBefore: String? = nil

        var id: String { key }

        enum CodingKeys: String, CodingKey {
            case key, text, day, fixes, tier, kind, override, cls, law
            case overrideAllowed = "override_allowed"
            case overridePrompt = "override_prompt"
            case overriddenBefore = "overridden_before"
        }
        enum OverrideKeys: String, CodingKey {
            case needs, candidates, prompt, why, legal
            case minReason = "min_reason"
        }
        enum MinReasonKeys: String, CodingKey { case chars, words }
        enum LegalKeys: String, CodingKey { case text }

        init(key: String, text: String, day: String? = nil, fixes: [Action] = [], overrideAllowed: Bool = true,
             tier: String? = nil, hard: Bool = true, overridePrompt: String? = nil, overriddenBefore: String? = nil) {
            self.key = key; self.text = text; self.day = day; self.fixes = fixes
            self.overrideAllowed = overrideAllowed; self.tier = tier; self.hard = hard
            self.overridePrompt = overridePrompt; self.overriddenBefore = overriddenBefore
            self.needsCoveredBy = Self.isManagerGap(key)
            self.overrideNeeds = needsCoveredBy ? "covered_by" : "reason"
        }

        /// The gate's manager-gap keys: `rule:no_manager:…`,
        /// `roster:no_manager_roster`.
        static func isManagerGap(_ key: String) -> Bool {
            key.hasPrefix("rule:no_manager") || key.hasPrefix("roster:no_manager")
        }

        /// The tier read as the colour: the must-fix tier (or a blocker from
        /// before the tiers, which the gate reads as must-fix) is red.
        static func isMustFixTier(_ tier: String?) -> Bool {
            guard let t = tier?.lowercased(), !t.isEmpty else { return true }
            return t == "must_fix"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let k = c.sfText(.key), let t = c.sfText(.text) else {
                throw DecodingError.dataCorruptedError(forKey: .key, in: c, debugDescription: "A must-fix with no key or text")
            }
            key = k
            text = t
            day = c.sfText(.day)
            fixes = c.setupList(Action.self, .fixes)
            overrideAllowed = c.sfBool(.overrideAllowed) ?? true
            tier = c.sfText(.tier)
            hard = Self.isMustFixTier(tier)
            cls = c.sfText(.cls)
            law = c.sfBool(.law) ?? false
            kind = c.sfText(.kind)
            overridePrompt = c.sfText(.overridePrompt)
            needsCoveredBy = Self.isManagerGap(key)
            overrideNeeds = needsCoveredBy ? "covered_by" : "reason"
            if let o = try? c.nestedContainer(keyedBy: OverrideKeys.self, forKey: .override) {
                if let needs = o.sfText(.needs) {
                    overrideNeeds = needs
                    needsCoveredBy = needs == "covered_by"
                }
                candidates = o.setupTexts(.candidates)
                overridePrompt = o.sfText(.prompt) ?? overridePrompt
                overrideWhy = o.sfText(.why)
                if let m = try? o.nestedContainer(keyedBy: MinReasonKeys.self, forKey: .minReason) {
                    minReasonChars = m.sfInt(.chars) ?? minReasonChars
                    minReasonWords = m.sfInt(.words) ?? minReasonWords
                }
                if let l = try? o.nestedContainer(keyedBy: LegalKeys.self, forKey: .legal) {
                    legalText = l.sfText(.text)
                }
            }
            overriddenBefore = reviewWords((try? c.decodeIfPresent(LooseValue.self, forKey: .overriddenBefore)) ?? nil)
                ?? Self.overriddenLine((try? c.decodeIfPresent(LooseValue.self, forKey: .overriddenBefore)) ?? nil)
        }

        /// publish-check's `{weeks, of, last}` in words.
        static func overriddenLine(_ v: LooseValue?) -> String? {
            guard case .object(let o)? = v, let weeks = reviewNumber(o["weeks"]), weeks > 0 else { return nil }
            let of = reviewNumber(o["of"]).map { " of \(Int($0))" } ?? ""
            return "You sent past this in \(Int(weeks))\(of) week\(weeks == 1 && of.isEmpty ? "" : "s")."
        }

        /// The posting law: sent past only with the owner's acknowledgement.
        var needsLegalAck: Bool { overrideNeeds == "legal_ack" }

        /// The question the reason field asks.
        var reasonPrompt: String {
            if let p = overridePrompt { return p }
            if needsCoveredBy { return "Which manager or owner runs the floor through it?" }
            if needsLegalAck { return legalText ?? "Acknowledge the law to send it anyway." }
            return "Why is it fine to send it anyway?"
        }

        /// Words are required unless a manager gap names who runs the floor.
        var needsReason: Bool { !needsCoveredBy }

        /// Why `reason` is not yet a reason the gate takes (TRUST-4), else
        /// nil: at least `minReasonChars` characters and `minReasonWords`
        /// words of letters — the server's own rule
        /// (schedule_overrides.reason_problem).
        func reasonProblem(_ reason: String) -> String? {
            let t = reason.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            if t.isEmpty { return "Say why it can go out anyway." }
            let words = t.split(whereSeparator: { !($0.isLetter || $0 == "'" || $0 == "\u{2019}" || $0 == "-") })
                .filter { $0.count >= 2 && ($0.first?.isLetter ?? false) }
            let distinct = Set(t.replacingOccurrences(of: " ", with: "").lowercased())
            if t.count < minReasonChars || words.count < minReasonWords || distinct.count < 3 {
                return "Say why in a few words \u{2014} it is kept with this week."
            }
            return nil
        }
    }

    /// One card per rule kind (TRUST-15): "17 people — too many days in a
    /// row" with its one group fix (the hard-rule pass) or, for decisions,
    /// one answer for all of them (`acknowledge_all`); the items stay in
    /// `must_fix` / `decide` for their own fixes and overrides.
    struct Group: Decodable, Equatable, Identifiable {
        var kind: String
        var tier: String? = nil
        var count = 1
        var keys: [String] = []
        var text: String
        var cls: String? = nil
        var law = false
        var overrideAllowed = true
        var fixes: [Action] = []
        var id: String { kind }

        enum CodingKeys: String, CodingKey {
            case kind, tier, count, keys, text, cls, law, fixes
            case overrideAllowed = "override_allowed"
        }

        init(kind: String, text: String, count: Int, keys: [String], fixes: [Action] = []) {
            self.kind = kind; self.text = text; self.count = count; self.keys = keys; self.fixes = fixes
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let t = c.sfText(.text) else {
                throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "A group with no text")
            }
            text = t
            kind = c.sfText(.kind) ?? t
            tier = c.sfText(.tier)
            count = c.sfInt(.count) ?? 1
            keys = c.setupTexts(.keys)
            cls = c.sfText(.cls)
            law = c.sfBool(.law) ?? false
            overrideAllowed = c.sfBool(.overrideAllowed) ?? true
            fixes = c.setupList(Action.self, .fixes)
        }
    }

    /// A warning this owner sends past nearly every week, said with a
    /// one-tap change to the rule behind it (TRUST-10).
    struct AlwaysOverridden: Decodable, Equatable, Identifiable {
        var kind: String
        var text: String
        var actions: [Action] = []
        var id: String { kind }

        enum CodingKeys: String, CodingKey { case kind, text, actions }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let t = c.sfText(.text) else {
                throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "Nothing said")
            }
            text = t
            kind = c.sfText(.kind) ?? t
            actions = c.setupList(Action.self, .actions)
        }
    }

    /// What holds Send and what does not, counted the way the web's headers
    /// and Summary tile count them (TRUST-16): a group of one rule's
    /// decisions is one.
    struct Counts: Decodable, Equatable {
        var mustFix: Int? = nil
        var decide: Int? = nil
        var decideHolding: Int? = nil
        var suggestions: Int? = nil
        var toFixOrDecide: Int? = nil
        var fyi: Int? = nil

        enum CodingKeys: String, CodingKey {
            case decide, suggestions, fyi
            case mustFix = "must_fix"
            case decideHolding = "decide_holding"
            case toFixOrDecide = "to_fix_or_decide"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            mustFix = c.sfInt(.mustFix); decide = c.sfInt(.decide); decideHolding = c.sfInt(.decideHolding)
            suggestions = c.sfInt(.suggestions); toFixOrDecide = c.sfInt(.toFixOrDecide); fyi = c.sfInt(.fyi)
        }

        /// What holds the send: the server's count, else must-fix plus the
        /// holding decisions.
        var holding: Int? {
            if let n = toFixOrDecide { return n }
            guard mustFix != nil || decideHolding != nil else { return nil }
            return (mustFix ?? 0) + (decideHolding ?? 0)
        }
    }

    /// A choice the owner makes in one tap; the answer is remembered in the
    /// scope the button states.
    struct Decision: Decodable, Equatable, Identifiable {
        var key: String
        var text: String
        var options: [Action] = []
        /// A publish-gate key (the budget, the notice rule, a late change):
        /// Send needs an answer, sent back as an acknowledgement. Others (a
        /// Crew shift leaner than usual) never hold the send.
        var holds = false
        var kind: String? = nil
        var overriddenBefore: String? = nil
        var id: String { key }

        enum CodingKeys: String, CodingKey {
            case key, text, options, holds, kind
            case overriddenBefore = "overridden_before"
        }

        init(key: String, text: String, options: [Action] = [], holds: Bool = false) {
            self.key = key; self.text = text; self.options = options; self.holds = holds
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let t = c.sfText(.text) else {
                throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "A decision with no text")
            }
            text = t
            key = c.sfText(.key) ?? t
            options = c.setupList(Action.self, .options)
            holds = c.sfBool(.holds) ?? false
            kind = c.sfText(.kind)
            overriddenBefore = MustFix.overriddenLine((try? c.decodeIfPresent(LooseValue.self, forKey: .overriddenBefore)) ?? nil)
        }
    }

    /// One figure with how sure it is: a value, a likely range, the basis it
    /// was worked out on and its track record.
    struct Figure: Decodable, Equatable {
        var value: Double? = nil
        var low: Double? = nil
        var high: Double? = nil
        var basis: String? = nil
        var trackRecord: String? = nil
        var projected = false
        var text: String? = nil
        var salaried: Double? = nil
        /// "All-in labor" / "Hourly labor" (TRUST-30) and the one grade both
        /// clients colour by: over | under | no_target (TRUST-7).
        var label: String? = nil
        var grade: String? = nil
        var target: Double? = nil
        /// "hourly" when the target is the hourly budget's share.
        var targetBasis: String? = nil
        /// Forecast sales: the sized-on figure against Home's, reconciled
        /// in words when they differ (ARCH-28).
        var reconcile: String? = nil

        enum CodingKeys: String, CodingKey {
            case value, low, high, basis, projected, text, total, hourly, salaried, label, grade, target, reconcile
            case trackRecord = "track_record"
            case targetBasis = "target_basis"
        }

        init(value: Double? = nil, low: Double? = nil, high: Double? = nil, basis: String? = nil,
             trackRecord: String? = nil, projected: Bool = false, text: String? = nil) {
            self.value = value; self.low = low; self.high = high; self.basis = basis
            self.trackRecord = trackRecord; self.projected = projected; self.text = text
        }

        init(from decoder: Decoder) throws {
            // A bare number is a figure with nothing said about it.
            if let single = try? decoder.singleValueContainer(), let n = try? single.decode(Double.self) {
                value = n.isFinite ? n : nil
                return
            }
            let c = try decoder.container(keyedBy: CodingKeys.self)
            value = c.sfDouble(.value) ?? c.sfDouble(.total) ?? c.sfDouble(.hourly)
            low = c.sfDouble(.low)
            high = c.sfDouble(.high)
            basis = c.sfText(.basis)
            trackRecord = reviewWords((try? c.decodeIfPresent(LooseValue.self, forKey: .trackRecord)) ?? nil)
            projected = c.sfBool(.projected) ?? false
            text = c.sfText(.text)
            salaried = c.sfDouble(.salaried)
            label = c.sfText(.label)
            grade = c.sfText(.grade)
            target = c.sfDouble(.target)
            targetBasis = c.sfText(.targetBasis)
            reconcile = c.sfText(.reconcile)
        }
    }

    /// The week's quality graded against the restaurant's own recent weeks
    /// (TRUST-29): the score, its grade, and why it was capped.
    struct Quality: Decodable, Equatable {
        var score: Int? = nil
        var grade: String? = nil
        var lead: String? = nil
        var text: String? = nil
        /// How much of the score was measured (LOGIC-15) and what the score
        /// has meant here: the restaurant's 78+ weeks against its under-65
        /// weeks as they RAN (TRUST-31) — nil below 3 weeks a side, said
        /// "—" with `outcomeNote` (the floor).
        var measuredText: String? = nil
        var outcomeText: String? = nil
        var outcomeNote: String? = nil
        enum CodingKeys: String, CodingKey {
            case score, grade, lead, text
            case measuredText = "measured_text"
            case outcomeText = "outcome_text"
            case outcomeLink = "outcome_link"
        }
        private enum LinkKeys: String, CodingKey { case note }
        init() {}
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            score = c.sfInt(.score); grade = c.sfText(.grade); lead = c.sfText(.lead); text = c.sfText(.text)
            measuredText = c.sfText(.measuredText)
            outcomeText = c.sfText(.outcomeText)
            outcomeNote = (try? c.nestedContainer(keyedBy: LinkKeys.self, forKey: .outcomeLink))?.sfText(.note)
        }

        /// "Your high- against low-scoring weeks: …", or "—" with the floor.
        var outcomeLine: String {
            if let outcomeText { return outcomeText }
            return "Your high- against low-scoring weeks: \u{2014}" + (outcomeNote.map { " (\($0))" } ?? "")
        }
    }

    struct Numbers: Decodable, Equatable {
        var laborPct: Figure? = nil
        var cost: Figure? = nil
        var overtimeHours: Double? = nil
        var forecastSales: Figure? = nil
        var savings: Figure? = nil
        /// `overtime.text` — what the overtime is, in words (null hours when
        /// it was not priced: no tile, never a green zero).
        var overtimeText: String? = nil
        /// `overtime` as priced: hours, the premium and whether it was
        /// priced at all — the Summary's Overtime tile reads it (TRUST-22).
        var overtimePriced: Bool? = nil
        var overtimePremium: Double? = nil
        /// Why there is no labor % and the real way to get one (TRUST-29):
        /// {reason, text, action} — never "generate a new draft" when no
        /// forecast exists to build it on.
        var laborMissing: Missing? = nil
        /// Last week's projection against what ran.
        var lastWeekText: String? = nil
        /// Scheduled against scheduled (blind re-audit 10/11/26 TRUST-7):
        /// this draft against the last published week's plan, priced the
        /// same way — withheld while any shift is short. `savings` is null
        /// since then.
        var vsLastPlan: VsLastPlan? = nil

        struct VsLastPlan: Codable, Hashable {
            var lastPct: Double? = nil
            var value: Double? = nil
            var text: String? = nil
            var withheld: String? = nil
            enum CodingKeys: String, CodingKey {
                case value, text, withheld
                case lastPct = "last_pct"
            }
            init(lastPct: Double? = nil, value: Double? = nil, text: String? = nil, withheld: String? = nil) {
                self.lastPct = lastPct; self.value = value; self.text = text; self.withheld = withheld
            }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                lastPct = c.sfDouble(.lastPct); value = c.sfDouble(.value)
                text = c.sfText(.text); withheld = c.sfText(.withheld)
            }
            /// "Under last week's plan" / "Over last week's plan".
            var label: String { (value ?? 0) < 0 ? "Over last week\u{2019}s plan" : "Under last week\u{2019}s plan" }
        }

        /// Why a figure is missing, said with the fix that would produce it.
        struct Missing: Decodable, Equatable {
            var reason: String? = nil
            var text: String
            var action: Action? = nil

            enum CodingKeys: String, CodingKey { case reason, text, action }

            init(reason: String? = nil, text: String, action: Action? = nil) {
                self.reason = reason; self.text = text; self.action = action
            }

            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                guard let t = c.sfText(.text) else {
                    throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "Nothing said")
                }
                text = t
                reason = c.sfText(.reason)
                action = (try? c.decodeIfPresent(Action.self, forKey: .action)) ?? nil
            }
        }

        private enum OvertimeKeys: String, CodingKey { case hours, premium, priced, text }

        enum CodingKeys: String, CodingKey {
            case cost, savings, overtime
            case laborPct = "labor_pct"
            case overtimeHours = "overtime_hours"
            case forecastSales = "forecast_sales"
            case laborMissing = "labor_missing"
            case lastWeek = "last_week"
            case vsLastPlan = "vs_last_plan"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            laborPct = (try? c.decodeIfPresent(Figure.self, forKey: .laborPct)) ?? nil
            cost = (try? c.decodeIfPresent(Figure.self, forKey: .cost)) ?? nil
            overtimeHours = c.sfDouble(.overtimeHours)
            forecastSales = (try? c.decodeIfPresent(Figure.self, forKey: .forecastSales)) ?? nil
            savings = (try? c.decodeIfPresent(Figure.self, forKey: .savings)) ?? nil
            if let o = try? c.nestedContainer(keyedBy: OvertimeKeys.self, forKey: .overtime) {
                overtimeText = o.sfText(.text)
                overtimePriced = o.sfBool(.priced)
                overtimePremium = o.sfDouble(.premium)
                if overtimeHours == nil, overtimePriced == true { overtimeHours = o.sfDouble(.hours) ?? 0 }
            }
            laborMissing = (try? c.decodeIfPresent(Missing.self, forKey: .laborMissing)) ?? nil
            lastWeekText = ((try? c.decodeIfPresent(Figure.self, forKey: .lastWeek)) ?? nil)?.text
            vsLastPlan = (try? c.decodeIfPresent(VsLastPlan.self, forKey: .vsLastPlan)) ?? nil
        }

        var isEmpty: Bool { laborPct?.value == nil && cost?.value == nil && overtimeHours == nil && forecastSales?.value == nil }
    }

    /// A line the review says, with the one tap it carries (if any). Read
    /// from a bare string or from {text, action}.
    struct Line: Decodable, Equatable, Identifiable {
        var text: String
        var action: Action? = nil
        var id: String { text }

        enum CodingKeys: String, CodingKey { case text, action, label }

        init(text: String, action: Action? = nil) { self.text = text; self.action = action }

        init(from decoder: Decoder) throws {
            if let single = try? decoder.singleValueContainer(), let s = try? single.decode(String.self) {
                let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty else {
                    throw DecodingError.dataCorruptedError(in: single, debugDescription: "An empty line")
                }
                text = t
                return
            }
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let t = c.sfText(.text) ?? c.sfText(.label) else {
                throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "A line with no text")
            }
            text = t
            action = (try? c.decodeIfPresent(Action.self, forKey: .action)) ?? nil
        }
    }

    struct Changed: Decodable, Equatable {
        var vsLastWeek: [Line] = []
        var vsUsual: [Line] = []
        var vsLastDraft: [Line] = []
        var cavnarChanges: [Line] = []

        enum CodingKeys: String, CodingKey {
            case vsLastWeek = "vs_last_week"
            case vsUsual = "vs_usual"
            case vsLastDraft = "vs_last_draft"
            case cavnarChanges = "cavnar_changes"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            vsLastWeek = c.setupList(Line.self, .vsLastWeek)
            vsUsual = c.setupList(Line.self, .vsUsual)
            vsLastDraft = c.setupList(Line.self, .vsLastDraft)
            cavnarChanges = c.setupList(Line.self, .cavnarChanges)
        }

        struct Group: Identifiable {
            let title: String
            let lines: [Line]
            var id: String { title }
        }

        /// The buckets with something in them, in reading order.
        var groups: [Group] {
            [Group(title: "Against last week", lines: vsLastWeek),
             Group(title: "Against your usual crew", lines: vsUsual),
             Group(title: "Against your last draft", lines: vsLastDraft),
             Group(title: "What Cavnar AI changed", lines: cavnarChanges)]
                .filter { !$0.lines.isEmpty }
        }

        var isEmpty: Bool { groups.isEmpty }
    }

    struct LeftOff: Decodable, Equatable, Identifiable {
        var name: String
        var reason: String
        var id: String { name }

        enum CodingKeys: String, CodingKey { case name, reason, employee }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let n = c.sfText(.name) ?? c.sfText(.employee) else {
                throw DecodingError.dataCorruptedError(forKey: .name, in: c, debugDescription: "Nobody named")
            }
            name = n
            reason = c.sfText(.reason) ?? ""
        }
    }

    /// One "why this person": the reason, where it came from (the engine's
    /// own reason for the pick, or one worked out after edits — TRUST-2),
    /// and, when a stronger runner-up could legally take it, the one-tap
    /// swap (TRUST-1): never a line that argues against the pick.
    struct Assignment: Decodable, Equatable, Identifiable {
        var text: String
        var basis: String? = nil
        var basisLabel: String? = nil
        var favoursPick: Bool? = nil
        var swap: Action? = nil
        var shiftId: String? = nil
        var id: String { (shiftId ?? "") + "|" + text }

        enum CodingKeys: String, CodingKey {
            case text, basis, swap
            case basisLabel = "basis_label"
            case favoursPick = "favours_pick"
            case shiftId = "shift_id"
        }

        init(text: String, basisLabel: String? = nil, swap: Action? = nil) {
            self.text = text; self.basisLabel = basisLabel; self.swap = swap
        }

        init(from decoder: Decoder) throws {
            if let single = try? decoder.singleValueContainer(), let s = try? single.decode(String.self) {
                let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty else { throw DecodingError.dataCorruptedError(in: single, debugDescription: "Empty") }
                text = t
                return
            }
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let t = c.sfText(.text) else {
                throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "A why with no text")
            }
            text = t
            basis = c.sfText(.basis)
            basisLabel = c.sfText(.basisLabel)
            favoursPick = c.sfBool(.favoursPick)
            swap = (try? c.decodeIfPresent(Action.self, forKey: .swap)) ?? nil
            shiftId = c.sfText(.shiftId)
        }
    }

    struct Why: Decodable, Equatable {
        var assignments: [Assignment] = []
        var leftOff: [LeftOff] = []
        /// Whether the engine's own reasons were recorded with this draft
        /// (`why.engine.text`) and how the draft was made (`why.draft.text`).
        var engineText: String? = nil
        var draftText: String? = nil

        enum CodingKeys: String, CodingKey {
            case assignments, engine, draft
            case leftOff = "left_off"
        }
        private enum TextKeys: String, CodingKey { case text }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            assignments = c.setupList(Assignment.self, .assignments)
            leftOff = c.setupList(LeftOff.self, .leftOff)
            engineText = (try? c.nestedContainer(keyedBy: TextKeys.self, forKey: .engine))?.sfText(.text)
            draftText = (try? c.nestedContainer(keyedBy: TextKeys.self, forKey: .draft))?.sfText(.text)
        }

        var isEmpty: Bool { assignments.isEmpty && leftOff.isEmpty && draftText == nil }
    }

    /// How sure: the share of past drafts sent unchanged and its trend, the
    /// edit predictor's hit rate against chance, and how complete the read
    /// was. Shares arrive as 0–1 or 0–100; `percent` reads either.
    struct Sure: Decodable, Equatable {
        var keptShare: Double? = nil
        var keptTrend: String? = nil
        var predictorHit: Double? = nil
        var predictorBase: Double? = nil
        var completeness: Double? = nil
        var text: String? = nil
        /// The server's own sentences, when it sends them — said as sent.
        var serverLines: [String] = []
        /// "How sure" (TRUST-6, TRUST-23): the K1 confidence over how the
        /// published weeks RAN, the same figure the web shows — nil below
        /// the sample floor, said "—" with what it still needs.
        var confidence: Confidence? = nil

        struct Confidence: Decodable, Equatable {
            var pct: Int? = nil
            var level: String? = nil
            var basis: [String] = []
            var meaning: String? = nil
            var weeksMeasured: Int? = nil
            /// The K1 object itself (Evidence, Accuracy, Freshness) for the
            /// "Why?" sheet; nil below the floor.
            var k1: TrustConfidence? = nil

            enum CodingKeys: String, CodingKey {
                case pct, level, basis, meaning, k1
                case weeksMeasured = "weeks_measured"
            }

            init(pct: Int? = nil, basis: [String] = [], meaning: String? = nil) {
                self.pct = pct; self.basis = basis; self.meaning = meaning
            }

            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                pct = c.sfInt(.pct)
                level = c.sfText(.level)
                basis = c.setupTexts(.basis)
                meaning = c.sfText(.meaning)
                weeksMeasured = c.sfInt(.weeksMeasured)
                k1 = (try? c.decodeIfPresent(TrustConfidence.self, forKey: .k1)) ?? nil
            }

            /// "72%", or "—" below the sample floor (never a guess).
            var figure: String { pct.map { "\($0)%" } ?? "\u{2014}" }

            /// What the figure rests on, the web's words: the basis joined,
            /// or "Nothing measured yet".
            var basisLine: String { basis.isEmpty ? "Nothing measured yet" : basis.joined(separator: " \u{00B7} ") }
        }

        enum CodingKeys: String, CodingKey {
            case completeness, text, confidence
            case serverLines = "lines"
            case keptShare = "kept_share"
            case keptTrend = "kept_trend"
            case predictorHit = "predictor_hit"
            case predictorBase = "predictor_base"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            keptShare = c.sfDouble(.keptShare)
            let trend = (try? c.decodeIfPresent(LooseValue.self, forKey: .keptTrend)) ?? nil
            keptTrend = reviewWords(trend)
            if keptTrend == nil, case .object(let o)? = trend {
                // {direction, older, newer}: "88% → 94%, rising".
                let older = Self.percent(reviewNumber(o["older"])), newer = Self.percent(reviewNumber(o["newer"]))
                let dir = reviewWords(o["direction"])
                if let older, let newer {
                    keptTrend = "\(older)% \u{2192} \(newer)%" + (dir.map { ", \($0)" } ?? "")
                }
            }
            serverLines = c.setupTexts(.serverLines)
            predictorHit = c.sfDouble(.predictorHit)
            predictorBase = c.sfDouble(.predictorBase)
            completeness = c.sfDouble(.completeness)
            text = c.sfText(.text)
            confidence = (try? c.decodeIfPresent(Confidence.self, forKey: .confidence)) ?? nil
        }

        /// 0.91 or 91 → 91.
        static func percent(_ v: Double?) -> Int? {
            guard let v, v.isFinite, v >= 0 else { return nil }
            return Int((v <= 1 ? v * 100 : v).rounded())
        }

        /// The lines the sheet says, each only when its figure came.
        var lines: [String] {
            if !serverLines.isEmpty { return serverLines }
            var out: [String] = []
            if let kept = Self.percent(keptShare) {
                var s = "Your recent drafts went out with \(kept)% of shifts unchanged"
                if let trend = keptTrend { s += " (\(trend))" }
                out.append(s + ".")
            }
            if let hit = Self.percent(predictorHit) {
                var s = "The shifts you usually change are marked; it guessed right \(hit)% of the time"
                if let base = Self.percent(predictorBase) { s += ", against \(base)% by chance" }
                out.append(s + ".")
            }
            if let done = Self.percent(completeness) {
                out.append("Checked on \(done)% of what it needs.")
            }
            if out.isEmpty, let text { out.append(text) }
            return out
        }
    }

    /// Who Send reaches, by channel.
    struct SendInfo: Decodable, Equatable {
        var app: Int? = nil
        var text: Int? = nil
        var email: Int? = nil
        var portalOnly: Int? = nil
        var label: String? = nil
        /// A week staff already have: the people a saved change moved —
        /// Send tells only them (the review carries it, so the sheet needs
        /// no second gate call, TRUST-25).
        var unsentChanges: [String]? = nil
        /// The publish outbox for a sent week: still sending, failed,
        /// unreachable (BUGSLIFE-21).
        var delivery: PublishDeliveryCounts? = nil

        enum CodingKeys: String, CodingKey {
            case reach, label, delivery
            case unsentChanges = "unsent_changes"
        }
        enum ReachKeys: String, CodingKey {
            case app, text, email
            case portalOnly = "portal_only"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            label = c.sfText(.label)
            if let r = try? c.nestedContainer(keyedBy: ReachKeys.self, forKey: .reach) {
                app = r.sfInt(.app); text = r.sfInt(.text); email = r.sfInt(.email); portalOnly = r.sfInt(.portalOnly)
            }
            if c.contains(.unsentChanges), (try? c.decodeNil(forKey: .unsentChanges)) != true {
                unsentChanges = c.setupTexts(.unsentChanges)
            }
            delivery = (try? c.decodeIfPresent(PublishDeliveryCounts.self, forKey: .delivery)) ?? nil
        }

        /// "15 in the app, 4 by text, 2 by email · 2 only in the staff portal".
        var line: String? {
            var bits: [String] = []
            if let n = app, n > 0 { bits.append("\(n) in the app") }
            if let n = text, n > 0 { bits.append("\(n) by text") }
            if let n = email, n > 0 { bits.append("\(n) by email") }
            var out = bits.joined(separator: ", ")
            if let n = portalOnly, n > 0 { out += (out.isEmpty ? "" : " \u{00B7} ") + "\(n) only in the staff portal" }
            return out.isEmpty ? nil : out
        }
    }

    var ok = false
    var scheduleId: Int? = nil
    var weekStart: String? = nil
    var weekEnd: String? = nil
    var verdict: Verdict? = nil
    var mustFix: [MustFix] = []
    var decide: [Decision] = []
    var numbers = Numbers()
    var changed = Changed()
    var fragile: [Line] = []
    var why = Why()
    var sure = Sure()
    var send = SendInfo()
    var error: String? = nil
    var version: Int? = nil
    var quality = Quality()
    /// Worth a look, never a hold (`fyi`: quality notes, soft rules by kind
    /// and day, reminders folded into one line).
    var fyi: [Line] = []
    /// The week as the gate reads it now — sent or not, whether this login
    /// may send, why a replaced copy can't be: the review carries what the
    /// publish check used to, so the sheet runs the gate once (TRUST-25).
    var publishedAt: String? = nil
    var canPublish: Bool? = nil
    var replacedReason: String? = nil
    /// One card per rule kind (TRUST-15) and the counts the web's headers
    /// use (TRUST-16).
    var mustFixGroups: [Group] = []
    var decideGroups: [Group] = []
    var counts = Counts()
    /// The rules this owner always sends past, each with a one-tap change
    /// (TRUST-10).
    var alwaysOverridden: [AlwaysOverridden] = []
    /// The week's hours split by pay (the gate's) and the rows the owner
    /// usually changes — once in this payload, never read again from the
    /// publish check.
    var hours: PublishHours? = nil
    var likelyToChange: LikelyToChange? = nil

    enum CodingKeys: String, CodingKey {
        case ok, verdict, decide, numbers, changed, fragile, why, sure, send, error, version, quality, fyi, counts, hours
        case scheduleId = "schedule_id"
        case weekStart = "week_start"
        case weekEnd = "week_end"
        case mustFix = "must_fix"
        case publishedAt = "published_at"
        case canPublish = "can_publish"
        case replacedReason = "replaced_reason"
        case mustFixGroups = "must_fix_groups"
        case decideGroups = "decide_groups"
        case alwaysOverridden = "always_overridden"
        case likelyToChange = "likely_to_change"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.sfBool(.ok) ?? false
        scheduleId = c.sfInt(.scheduleId)
        weekStart = c.sfText(.weekStart)
        weekEnd = c.sfText(.weekEnd)
        verdict = (try? c.decodeIfPresent(Verdict.self, forKey: .verdict)) ?? nil
        mustFix = c.setupList(MustFix.self, .mustFix)
        decide = c.setupList(Decision.self, .decide)
        numbers = ((try? c.decodeIfPresent(Numbers.self, forKey: .numbers)) ?? nil) ?? Numbers()
        changed = ((try? c.decodeIfPresent(Changed.self, forKey: .changed)) ?? nil) ?? Changed()
        fragile = c.setupList(Line.self, .fragile)
        why = ((try? c.decodeIfPresent(Why.self, forKey: .why)) ?? nil) ?? Why()
        sure = ((try? c.decodeIfPresent(Sure.self, forKey: .sure)) ?? nil) ?? Sure()
        send = ((try? c.decodeIfPresent(SendInfo.self, forKey: .send)) ?? nil) ?? SendInfo()
        error = c.sfText(.error)
        version = c.sfInt(.version)
        quality = ((try? c.decodeIfPresent(Quality.self, forKey: .quality)) ?? nil) ?? Quality()
        fyi = c.setupList(Line.self, .fyi)
        publishedAt = c.sfText(.publishedAt)
        canPublish = c.sfBool(.canPublish)
        replacedReason = c.sfText(.replacedReason)
        mustFixGroups = c.setupList(Group.self, .mustFixGroups)
        decideGroups = c.setupList(Group.self, .decideGroups)
        counts = ((try? c.decodeIfPresent(Counts.self, forKey: .counts)) ?? nil) ?? Counts()
        alwaysOverridden = c.setupList(AlwaysOverridden.self, .alwaysOverridden)
        hours = (try? c.decodeIfPresent(PublishHours.self, forKey: .hours)) ?? nil
        likelyToChange = (try? c.decodeIfPresent(LikelyToChange.self, forKey: .likelyToChange)) ?? nil
    }

    /// The decisions Send waits on, answered or not.
    var holding: [Decision] { decide.filter(\.holds) }

    /// The decisions that never hold Send (TRUST-16): said apart, under
    /// "Suggestions".
    var suggestions: [Decision] { decide.filter { !$0.holds } }

    /// One group card with the items it folds.
    struct Folded<T>: Identifiable {
        let group: Group
        let members: [T]
        var id: String { group.id }
    }

    /// The groups of more than one item, with the items each folds (the
    /// rest are shown on their own) — the web's mustHtml / decideHtml.
    static func folded<T>(_ items: [T], groups: [Group], key: (T) -> String) -> (groups: [Folded<T>], rest: [T]) {
        var used = Set<String>()
        var out: [Folded<T>] = []
        for g in groups where g.count > 1 {
            let members = items.filter { g.keys.contains(key($0)) }
            guard members.count > 1 else { continue }
            members.forEach { used.insert(key($0)) }
            out.append(Folded(group: g, members: members))
        }
        return (out, items.filter { !used.contains(key($0)) })
    }

    /// Read as the review only when it says so and names its week.
    var isUsable: Bool { ok && verdict != nil }
}

// MARK: - Overrides (TRUST-28, the contract's `overrides: [{key, reason}]`)

/// One must-fix sent past on purpose, with the owner's words — stored by
/// key on the server and shown in History.
struct PublishOverride: Encodable, Equatable {
    let key: String
    let reason: String
    /// Who runs the floor through a manager gap (ARCH-30): kept on the
    /// override record with the published version.
    var coveredBy: String? = nil
    /// The owner acknowledged the law it goes out past (TRUST-8).
    var legalAck = false

    enum CodingKeys: String, CodingKey {
        case key, reason
        case coveredBy = "covered_by"
        case legalAck = "legal_ack"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(key, forKey: .key)
        try c.encode(reason, forKey: .reason)
        try c.encodeIfPresent(coveredBy, forKey: .coveredBy)
        if legalAck { try c.encode(true, forKey: .legalAck) }
    }
}

/// What the owner said when sending past one item: their reason and, for a
/// manager gap, who runs the floor; for the posting law, the acknowledgement.
struct PublishOverrideChoice: Equatable {
    var reason: String
    var coveredBy: String? = nil
    var legalAck = false
}

/// The publish outbox's counts for a sent week (schedule_publish.
/// notice_status): still sending, failed (retried), unreachable.
struct PublishDeliveryCounts: Decodable, Equatable {
    var sending: Int? = nil
    var failed: Int? = nil
    var unreachable: Int? = nil

    enum CodingKeys: String, CodingKey { case sending, failed, unreachable }

    init(sending: Int? = nil, failed: Int? = nil, unreachable: Int? = nil) {
        self.sending = sending; self.failed = failed; self.unreachable = unreachable
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sending = c.sfInt(.sending); failed = c.sfInt(.failed); unreachable = c.sfInt(.unreachable)
    }

    /// The people the first send did not reach (failed or unreachable).
    var missing: Int { (failed ?? 0) + (unreachable ?? 0) }
}

/// Where a review's "open" tap goes (schedule re-audit 10/11/26 TRUST-20):
/// the screen its payload names, never a sheet that just closes.
enum ReviewOpenTarget: Equatable {
    /// Redo these days (`redo`).
    case redo([String])
    /// Who runs the floor: Setup's roster, where each person's "runs the
    /// floor" is set (`panel: managers`).
    case managers
    /// One person's record in Team (`stage: team, employee`).
    case person(String)
    /// One shift on the week (`stage: schedule` with a shift or a date).
    case shift(date: String?, shiftId: String?, employee: String?, start: String?)
    /// A shift to find for somebody left off (`stage: schedule, employee`
    /// alone).
    case addShift(String)
    /// A setting the phone sets on the web (the rules, the budget).
    case web(path: String)
    /// The restaurant's targets — a weekly sales estimate to forecast from
    /// when no sales are connected (TRUST-29).
    case targets

    /// The target an `open` payload names; nil when it names nothing.
    static func from(_ p: [String: LooseValue]) -> ReviewOpenTarget? {
        func text(_ k: String) -> String? { LaborViewModel.payloadText(p, k) }
        if case .list(let l)? = p["redo"] {
            let dates = l.compactMap { v -> String? in
                if case .string(let s) = v { return String(s.prefix(10)) }
                return nil
            }.filter { !$0.isEmpty }
            if !dates.isEmpty { return .redo(dates) }
        }
        let stage = (text("stage") ?? "schedule").lowercased()
        let panel = text("panel")?.lowercased()
        if panel == "managers" { return .managers }
        if panel == "targets" { return .targets }
        switch stage {
        case "team":
            if let who = text("employee") { return .person(who) }
            return .managers
        case "setup":
            switch panel {
            case "rules": return .web(path: "labor/rules")
            default: return .web(path: "labor/schedule")
            }
        default:
            let date = text("date").map { String($0.prefix(10)) }
            let sid = text("shift_id"), who = text("employee")
            if date == nil && sid == nil, let who { return .addShift(who) }
            if date == nil && sid == nil { return nil }
            return .shift(date: date, shiftId: sid, employee: who, start: text("shift_start"))
        }
    }
}

/// The must-fix list as the sheet acts on it: what still holds Send, and
/// the overrides to send with it. Pure, so it can be tested on its own.
enum PublishGate {
    /// Whether one choice is enough for its item, as the gate checks it
    /// (schedule_publish.override_problem): a manager gap names one of the
    /// managers or owners on the roster who can work that day (TRUST-5); the
    /// posting law is acknowledged with a reason (TRUST-8); anything else
    /// carries a reason of a few words (TRUST-4); a law an edit can fix is
    /// never sent past.
    static func complete(_ item: PublishReview.MustFix, _ choice: PublishOverrideChoice?) -> Bool {
        guard item.overrideAllowed, let choice else { return false }
        if item.needsCoveredBy {
            let who = (choice.coveredBy ?? "").trimmingCharacters(in: .whitespaces)
            // Only a name the gate offered — a typed name is refused (409)
            // unless it is one of them; with none, the gap is fixed on the
            // week or a manager is named in Team (the web's rule too).
            guard !who.isEmpty else { return false }
            return item.candidates.contains { $0.caseInsensitiveCompare(who) == .orderedSame }
        }
        if item.needsLegalAck && !choice.legalAck { return false }
        return item.reasonProblem(choice.reason) == nil
    }

    /// The items still holding Send: neither fixed (gone from the list) nor
    /// overridden with what the override needs.
    static func open(_ items: [PublishReview.MustFix],
                     overrides: [String: PublishOverrideChoice]) -> [PublishReview.MustFix] {
        items.filter { !complete($0, overrides[$0.key]) }
    }

    /// The overrides to send: only for items still on the list, in its
    /// order — an override for something since fixed is never sent. A
    /// manager gap's reason, left blank, says who runs the floor (the gate
    /// asks for a reason on every must-fix).
    static func overrides(_ items: [PublishReview.MustFix],
                          _ chosen: [String: PublishOverrideChoice]) -> [PublishOverride] {
        items.compactMap { item in
            guard complete(item, chosen[item.key]), let c = chosen[item.key] else { return nil }
            let who = c.coveredBy?.trimmingCharacters(in: .whitespaces)
            var words = c.reason.trimmingCharacters(in: .whitespacesAndNewlines)
            if words.isEmpty, let who, !who.isEmpty, item.needsCoveredBy { words = "\(who) runs the floor" }
            return PublishOverride(key: item.key, reason: String(words.prefix(300)),
                                   coveredBy: item.needsCoveredBy ? who : nil,
                                   legalAck: item.needsLegalAck && c.legalAck)
        }
    }

    /// The Send button's words: "Fix 2 things first", or "Send to 21 staff
    /// (overriding 1 rule)".
    static func sendLabel(open: Int, overriding: Int, reachable: Int, serverLabel: String?,
                          undecided: Int = 0) -> String {
        if open > 0 { return open == 1 ? "Fix 1 thing first" : "Fix \(open) things first" }
        if undecided > 0 { return undecided == 1 ? "Decide 1 thing first" : "Decide \(undecided) things first" }
        let base = serverLabel ?? (reachable > 0 ? "Send to \(reachable) staff" : "Publish to the staff portal")
        guard overriding > 0 else { return base }
        return base + " (overriding \(overriding) rule\(overriding == 1 ? "" : "s"))"
    }
}

// MARK: - Fixes that edit the week on screen

extension LaborViewModel {
    /// A text field of an action's payload.
    nonisolated static func payloadText(_ p: [String: LooseValue], _ keys: String...) -> String? {
        for k in keys {
            switch p[k] {
            case .string(let s)?:
                let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
                if !t.isEmpty { return t }
            case .number(let n)?:
                return n == n.rounded() ? String(Int(n)) : String(n)
            default:
                continue
            }
        }
        return nil
    }

    /// The row an action's payload names: by its shift id, else by person,
    /// date and start (the start compared as a time, "4pm" = "4:00pm").
    nonisolated static func rowFor(_ p: [String: LooseValue], in rows: [ScheduleRow]) -> ScheduleRow? {
        if let sid = payloadText(p, "shift_id")?.lowercased(),
           let hit = rows.first(where: { ($0.shiftId ?? "").lowercased() == sid }) {
            return hit
        }
        guard let who = payloadText(p, "employee", "name")?.lowercased(),
              let date = payloadText(p, "date").map({ String($0.prefix(10)) }) else { return nil }
        let start = shiftMinutes(payloadText(p, "shift_start", "start"))
        let same = rows.filter {
            ($0.employee ?? "").lowercased() == who && String(($0.date ?? "").prefix(10)) == date
        }
        if let start, let hit = same.first(where: { shiftMinutes($0.shiftStart) == start }) { return hit }
        return same.count == 1 ? same[0] : nil
    }

    /// The actions that change the rows on screen — the same edit, re-score
    /// and save as a hand edit. Everything else is the sheet's to send.
    nonisolated static let rowActions: Set<String> = [
        "edit_rows", "optimize", "apply_fixes",
        "extend_shift", "set_end", "end_at", "retime", "set_times", "move_start",
        "reassign", "give_to", "swap_to", "remove_shift", "drop_shift", "add_shift", "add_person",
    ]

    /// `edit_rows` {add: [row], remove: [match], change: [{match, set}]} —
    /// the review's fix as edits on the week on screen, each the same edit,
    /// re-score and save as a hand edit. Nil when every part was made.
    func applyEditRows(_ p: [String: LooseValue]) async -> String? {
        func objects(_ key: String) -> [[String: LooseValue]] {
            guard case .list(let l)? = p[key] else { return [] }
            return l.compactMap { if case .object(let o) = $0 { return o } else { return nil } }
        }
        for change in objects("change") {
            var match: [String: LooseValue] = [:]
            if case .object(let m)? = change["match"] { match = m }
            var set: [String: LooseValue] = [:]
            if case .object(let s)? = change["set"] { set = s }
            guard let row = Self.rowFor(match, in: scheduleResult?.previewRows ?? []) else {
                return "That shift is no longer on the week."
            }
            if let who = Self.payloadText(set, "employee"), who.lowercased() != (row.employee ?? "").lowercased() {
                if let why = weekReadOnlyReason { return why }
                await overrideEmployee(rowId: row.id, to: who)
            }
            let start = Self.payloadText(set, "shift_start"), end = Self.payloadText(set, "shift_end")
            if start != nil || end != nil {
                let now = Self.rowFor(match.merging(set.filter { $0.key == "employee" }) { _, new in new },
                                      in: scheduleResult?.previewRows ?? []) ?? row
                if let refusal = await editShiftTimes(rowId: now.id, start: start ?? now.shiftStart ?? "",
                                                      end: end ?? now.shiftEnd ?? "") {
                    return refusal
                }
            }
        }
        for match in objects("remove") {
            guard let row = Self.rowFor(match, in: scheduleResult?.previewRows ?? []) else { continue }
            if let why = weekReadOnlyReason { return why }
            await removeShift(rowId: row.id)
        }
        for row in objects("add") {
            if let refusal = await applyReviewAction(PublishReview.Action(label: "Add", action: "add_shift", payload: row)) {
                return refusal
            }
        }
        return nil
    }

    /// Do one review action on the week on screen. Nil when it was made;
    /// otherwise the reason it was not (said under the button).
    func applyReviewAction(_ action: PublishReview.Action) async -> String? {
        let p = action.payload
        let rows = scheduleResult?.previewRows ?? []
        switch action.action {
        case "edit_rows":
            return await applyEditRows(p)
        case "optimize":
            // Improve shows each change; nothing is kept until it is saved.
            await optimize()
            return optimizeError ?? "Cavnar AI\u{2019}s changes are on the week \u{2014} save them on the Labor screen, then send."
        case "apply_fixes":
            await applyFixes()
            return applyFixesError ?? "The fixes are on the week \u{2014} save them on the Labor screen, then send."
        case "extend_shift", "set_end", "end_at", "retime", "set_times", "move_start":
            guard let row = Self.rowFor(p, in: rows) else { return "That shift is no longer on the week." }
            let start = Self.payloadText(p, "new_start") ?? row.shiftStart ?? ""
            let end = Self.payloadText(p, "new_end", "until", "end_at") ?? row.shiftEnd ?? ""
            return await editShiftTimes(rowId: row.id, start: start, end: end)
        case "reassign", "give_to", "swap_to":
            guard let row = Self.rowFor(p, in: rows) else { return "That shift is no longer on the week." }
            guard let to = Self.payloadText(p, "to", "new_employee", "employee_to", "give_to") else {
                return "Nobody was named to take it."
            }
            if let why = weekReadOnlyReason { return why }
            await overrideEmployee(rowId: row.id, to: to)
            return nil
        case "remove_shift", "drop_shift":
            guard let row = Self.rowFor(p, in: rows) else { return "That shift is no longer on the week." }
            if let why = weekReadOnlyReason { return why }
            await removeShift(rowId: row.id)
            return nil
        case "add_shift", "add_person":
            var q = p
            if case .object(let row)? = p["row"] { q = row }
            guard let date = Self.payloadText(q, "date"), let who = Self.payloadText(q, "employee", "name"),
                  let start = Self.payloadText(q, "shift_start", "start"),
                  let end = Self.payloadText(q, "shift_end", "end") else {
                return "That shift couldn\u{2019}t be read."
            }
            return await addShift(date: String(date.prefix(10)), employee: who, role: Self.payloadText(q, "role"),
                                  start: start, end: end)
        default:
            return "Open the week to do this one."
        }
    }
}
