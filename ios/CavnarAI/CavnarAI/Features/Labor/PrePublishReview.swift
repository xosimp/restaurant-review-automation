import Foundation

// The pre-publish review (schedule audit 10/10/26 TRUST-27, RM-6): ONE
// server payload — GET /mobile/api/labor/publish-review?schedule_id= (the
// twin of the web's /api/labor/publish-review) — drives the web's "Before
// it goes out" and the phone's Send sheet, so the two can never disagree.
// The contract (SHARED #6): {ok, schedule_id, week_start, week_end,
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

        enum CodingKeys: String, CodingKey { case tone, sentence, subline, text }

        init(tone: String, sentence: String, subline: String? = nil) {
            self.tone = tone; self.sentence = sentence; self.subline = subline
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            tone = (c.sfText(.tone) ?? "amber").lowercased()
            sentence = c.sfText(.sentence) ?? c.sfText(.text) ?? ""
            subline = c.sfText(.subline)
        }
    }

    /// Something that must be fixed — or overridden, on its own, with a
    /// reason — before the week goes out. `hard` items are the owner's
    /// non-negotiables and the law: red, never amber (TRUST-27).
    struct MustFix: Decodable, Equatable, Identifiable {
        var key: String
        var text: String
        var day: String? = nil
        var fixes: [Action] = []
        var overrideAllowed = true
        var tier: String? = nil
        var hard = true
        /// What the override's reason field asks ("Who covers? The owner
        /// counts." for a manager gap — ARCH-30). Nil: the plain question.
        var overridePrompt: String? = nil
        /// "You sent past this in 3 of 4 weeks" (TRUST-28), when the server
        /// says so.
        var overriddenBefore: String? = nil

        var id: String { key }

        enum CodingKeys: String, CodingKey {
            case key, text, day, fixes, tier, hard, kind
            case overrideAllowed = "override_allowed"
            case overridePrompt = "override_prompt"
            case overriddenBefore = "overridden_before"
        }

        init(key: String, text: String, day: String? = nil, fixes: [Action] = [], overrideAllowed: Bool = true,
             tier: String? = nil, hard: Bool = true, overridePrompt: String? = nil, overriddenBefore: String? = nil) {
            self.key = key; self.text = text; self.day = day; self.fixes = fixes
            self.overrideAllowed = overrideAllowed; self.tier = tier; self.hard = hard
            self.overridePrompt = overridePrompt; self.overriddenBefore = overriddenBefore
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
            // Hard unless the server says otherwise: a must-fix the phone
            // cannot place is read as the serious kind, never the soft one.
            if let h = c.sfBool(.hard) {
                hard = h
            } else if let t = tier?.lowercased() {
                hard = !(t == "soft" || t == "budget" || t == "notice" || t == "fyi")
            } else {
                hard = true
            }
            overridePrompt = c.sfText(.overridePrompt)
            overriddenBefore = reviewWords((try? c.decodeIfPresent(LooseValue.self, forKey: .overriddenBefore)) ?? nil)
                ?? Self.overriddenLine((try? c.decodeIfPresent(LooseValue.self, forKey: .overriddenBefore)) ?? nil)
        }

        /// publish-check's `{weeks, of, last}` in words.
        static func overriddenLine(_ v: LooseValue?) -> String? {
            guard case .object(let o)? = v, let weeks = reviewNumber(o["weeks"]), weeks > 0 else { return nil }
            let of = reviewNumber(o["of"]).map { " of \(Int($0))" } ?? ""
            return "You sent past this in \(Int(weeks))\(of) week\(weeks == 1 && of.isEmpty ? "" : "s")."
        }

        /// The question the reason field asks.
        var reasonPrompt: String {
            if let p = overridePrompt { return p }
            if key.hasPrefix("rule:no_manager") { return "Who covers as manager? The owner counts." }
            return "Why is it all right this week?"
        }

        /// A hard item needs words before it can be sent past.
        var needsReason: Bool { hard }
    }

    /// A choice the owner makes in one tap; the answer is remembered in the
    /// scope the button states.
    struct Decision: Decodable, Equatable, Identifiable {
        var key: String
        var text: String
        var options: [Action] = []
        var id: String { key }

        enum CodingKeys: String, CodingKey { case key, text, options }

        init(key: String, text: String, options: [Action] = []) {
            self.key = key; self.text = text; self.options = options
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let t = c.sfText(.text) else {
                throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "A decision with no text")
            }
            text = t
            key = c.sfText(.key) ?? t
            options = c.setupList(Action.self, .options)
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

        enum CodingKeys: String, CodingKey {
            case value, low, high, basis, projected, text, total, hourly, salaried
            case trackRecord = "track_record"
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
        }
    }

    struct Numbers: Decodable, Equatable {
        var laborPct: Figure? = nil
        var cost: Figure? = nil
        var overtimeHours: Double? = nil
        var forecastSales: Figure? = nil
        var savings: Figure? = nil

        enum CodingKeys: String, CodingKey {
            case cost, savings
            case laborPct = "labor_pct"
            case overtimeHours = "overtime_hours"
            case forecastSales = "forecast_sales"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            laborPct = (try? c.decodeIfPresent(Figure.self, forKey: .laborPct)) ?? nil
            cost = (try? c.decodeIfPresent(Figure.self, forKey: .cost)) ?? nil
            overtimeHours = c.sfDouble(.overtimeHours)
            forecastSales = (try? c.decodeIfPresent(Figure.self, forKey: .forecastSales)) ?? nil
            savings = (try? c.decodeIfPresent(Figure.self, forKey: .savings)) ?? nil
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

    struct Why: Decodable, Equatable {
        var assignments: [Line] = []
        var leftOff: [LeftOff] = []

        enum CodingKeys: String, CodingKey {
            case assignments
            case leftOff = "left_off"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            assignments = c.setupList(Line.self, .assignments)
            leftOff = c.setupList(LeftOff.self, .leftOff)
        }

        var isEmpty: Bool { assignments.isEmpty && leftOff.isEmpty }
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

        enum CodingKeys: String, CodingKey {
            case completeness, text
            case keptShare = "kept_share"
            case keptTrend = "kept_trend"
            case predictorHit = "predictor_hit"
            case predictorBase = "predictor_base"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            keptShare = c.sfDouble(.keptShare)
            keptTrend = reviewWords((try? c.decodeIfPresent(LooseValue.self, forKey: .keptTrend)) ?? nil)
            predictorHit = c.sfDouble(.predictorHit)
            predictorBase = c.sfDouble(.predictorBase)
            completeness = c.sfDouble(.completeness)
            text = c.sfText(.text)
        }

        /// 0.91 or 91 → 91.
        static func percent(_ v: Double?) -> Int? {
            guard let v, v.isFinite, v >= 0 else { return nil }
            return Int((v <= 1 ? v * 100 : v).rounded())
        }

        /// The lines the sheet says, each only when its figure came.
        var lines: [String] {
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

        enum CodingKeys: String, CodingKey { case reach, label }
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

    enum CodingKeys: String, CodingKey {
        case ok, verdict, decide, numbers, changed, fragile, why, sure, send, error
        case scheduleId = "schedule_id"
        case weekStart = "week_start"
        case weekEnd = "week_end"
        case mustFix = "must_fix"
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
}

/// The must-fix list as the sheet acts on it: what still holds Send, and
/// the overrides to send with it. Pure, so it can be tested on its own.
enum PublishGate {
    /// The items still holding Send: neither fixed (gone from the list) nor
    /// overridden — and a hard item's override needs a reason.
    static func open(_ items: [PublishReview.MustFix], overrides: [String: String]) -> [PublishReview.MustFix] {
        items.filter { item in
            guard item.overrideAllowed, let reason = overrides[item.key] else { return true }
            return item.needsReason && reason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    /// The overrides to send: only for items still on the list, in its
    /// order — an override for something since fixed is never sent.
    static func overrides(_ items: [PublishReview.MustFix], _ chosen: [String: String]) -> [PublishOverride] {
        items.compactMap { item in
            guard item.overrideAllowed, let reason = chosen[item.key] else { return nil }
            let words = reason.trimmingCharacters(in: .whitespacesAndNewlines)
            if item.needsReason && words.isEmpty { return nil }
            return PublishOverride(key: item.key, reason: String(words.prefix(300)))
        }
    }

    /// The Send button's words: "Fix 2 things first", or "Send to 21 staff
    /// (overriding 1 rule)".
    static func sendLabel(open: Int, overriding: Int, reachable: Int, serverLabel: String?) -> String {
        if open > 0 { return open == 1 ? "Fix 1 thing first" : "Fix \(open) things first" }
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
        "extend_shift", "set_end", "end_at", "retime", "set_times", "move_start",
        "reassign", "give_to", "swap_to", "remove_shift", "drop_shift", "add_shift", "add_person",
    ]

    /// Do one review action on the week on screen. Nil when it was made;
    /// otherwise the reason it was not (said under the button).
    func applyReviewAction(_ action: PublishReview.Action) async -> String? {
        let p = action.payload
        let rows = scheduleResult?.previewRows ?? []
        switch action.action {
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
