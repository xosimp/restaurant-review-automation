import Foundation

// The generation, review, publish-check and learning payloads the schedule
// fix round (schedule audit 10/3/26) added, as the phone reads them. Every
// type decodes leniently — a missing key is nil or empty, a malformed entry
// is skipped — because each rides on GeneratedSchedule, which is also the
// on-device cache: one odd field must never fail the whole week (B2 found
// finished generations failing to decode on `chunked: 1`).

extension KeyedDecodingContainer {
    /// Text, trimmed; a number is read as its text. Nil when absent or empty.
    func sfText(_ key: Key) -> String? {
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil {
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? nil : t
        }
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return String(i) }
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite {
            return d == d.rounded() ? String(Int(d)) : String(d)
        }
        return nil
    }

    func sfInt(_ key: Key) -> Int? {
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i }
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return Int(d.rounded()) }
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil { return Int(s) }
        return nil
    }

    func sfDouble(_ key: Key) -> Double? {
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return d }
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil { return Double(s) }
        return nil
    }

    /// A flag sent as a bool or as 0/1.
    func sfBool(_ key: Key) -> Bool? {
        if let b = (try? decodeIfPresent(Bool.self, forKey: key)) ?? nil { return b }
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i != 0 }
        return nil
    }

    func sfList<T: Codable & Hashable>(_ type: T.Type, _ key: Key) -> [T] {
        ((try? decodeIfPresent(HomeLenientList<T>.self, forKey: key)) ?? nil)?.items ?? []
    }

    /// A list of words; a single string is a list of one.
    func sfWords(_ key: Key) -> [String] {
        if let a = (try? decodeIfPresent([String].self, forKey: key)) ?? nil {
            return a.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        }
        if let s = sfText(key) { return [s] }
        return []
    }
}

extension CavnarDate {
    /// "Saturday 10/10/26" — a weekday with its date, as every banner names a
    /// day. The date alone when no weekday came with it.
    static func dayDate(_ day: String?, _ iso: String?) -> String {
        let d = iso.map { mdy($0) } ?? ""
        guard let day, !day.isEmpty else { return d }
        return d.isEmpty ? day : "\(day) \(d)"
    }

    /// "Saturday 10/10/26 and Sunday 10/11/26" — a list of days in words.
    static func dayList(_ items: [String]) -> String {
        switch items.count {
        case 0: return ""
        case 1: return items[0]
        default: return items.dropLast().joined(separator: ", ") + " and " + items[items.count - 1]
        }
    }
}

// MARK: - Manager plan (schedule_skeleton.payload, generate result `manager_plan`)

/// The managers' shifts planned before the draft was written, each date's
/// manager window, the stretches nobody could legally cover, standing
/// shifts that could not be used, and the owner's question about managers
/// whose days are unknown (schedule audit 10/3/26 D-5, M-1…M-6).
struct ManagerPlan: Codable, Hashable {
    var planned = false
    var failed = false
    var shifts: [Shift] = []
    var windows: [String: Window] = [:]
    var uncovered: [Uncovered] = []
    var skipped: [Skipped] = []
    var unknownPattern: [String] = []
    var question: String?

    struct Shift: Codable, Hashable {
        var date: String?
        var day: String?
        var employee: String?
        var role: String?
        var shiftStart: String?
        var shiftEnd: String?
        var hours: Double?
        var source: String?
        var reason: String?

        enum CodingKeys: String, CodingKey {
            case date, day, employee, role, hours, source, reason
            case shiftStart = "shift_start"
            case shiftEnd = "shift_end"
        }

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            date = c.sfText(.date); day = c.sfText(.day); employee = c.sfText(.employee)
            role = c.sfText(.role); shiftStart = c.sfText(.shiftStart); shiftEnd = c.sfText(.shiftEnd)
            hours = c.sfDouble(.hours); source = c.sfText(.source); reason = c.sfText(.reason)
        }
    }

    /// "11:00am" to "11:00pm" — when a manager has to be on that day.
    struct Window: Codable, Hashable {
        var from: String?
        var to: String?
        var source: String?

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            from = c.sfText(.from); to = c.sfText(.to); source = c.sfText(.source)
        }
        enum CodingKeys: String, CodingKey { case from, to, source }
    }

    /// A stretch no manager could legally cover, and why.
    struct Uncovered: Codable, Hashable {
        var date: String?
        var day: String?
        var from: String?
        var to: String?
        var minutes: Int?
        var why: String?

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            date = c.sfText(.date); day = c.sfText(.day); from = c.sfText(.from); to = c.sfText(.to)
            minutes = c.sfInt(.minutes); why = c.sfText(.why)
        }
        enum CodingKeys: String, CodingKey { case date, day, from, to, minutes, why }
    }

    /// A standing shift the plan could not use, with why.
    struct Skipped: Codable, Hashable {
        var employee: String?
        var date: String?
        var shift: String?
        var why: String?

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            employee = c.sfText(.employee); date = c.sfText(.date); shift = c.sfText(.shift); why = c.sfText(.why)
        }
        enum CodingKeys: String, CodingKey { case employee, date, shift, why }

        /// "Erik's standing shift on Tuesday 10/6/26 wasn't used — their note: no Tuesdays this month."
        var line: String {
            let who = employee ?? "A manager"
            let day = date.flatMap { LaborViewModel.weekdayName($0) }
            var s = "\(who)\u{2019}s standing shift on \(CavnarDate.dayDate(day, date))"
            if let shift { s += " (\(shift))" }
            s += " wasn\u{2019}t used"
            if let why { s += " \u{2014} \(why)" }
            return s.hasSuffix(".") ? s : s + "."
        }
    }

    enum CodingKeys: String, CodingKey {
        case planned, failed, shifts, windows, uncovered, skipped, question
        case unknownPattern = "unknown_pattern"
    }

    init() {}

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        planned = c.sfBool(.planned) ?? false
        failed = c.sfBool(.failed) ?? false
        shifts = c.sfList(Shift.self, .shifts)
        windows = ((try? c.decodeIfPresent([String: Window].self, forKey: .windows)) ?? nil) ?? [:]
        uncovered = c.sfList(Uncovered.self, .uncovered)
        skipped = c.sfList(Skipped.self, .skipped)
        unknownPattern = c.sfWords(.unknownPattern)
        question = c.sfText(.question)
    }

    /// "Manager on 11:00am–11:00pm" for one date, when the plan set a window.
    func windowLine(for date: String?) -> String? {
        guard let date, let w = windows[String(date.prefix(10))], let from = w.from, let to = w.to else { return nil }
        return "Manager on \(from)\u{2013}\(to)"
    }
}

// MARK: - The manager rule's backstop (`manager_coverage`)

struct ManagerCoverage: Codable, Hashable {
    var extended: Int?
    var added: Int?
    var left: [Left] = []
    var shortfall: Shortfall?

    /// A stretch still without a manager, why each manager could not cover
    /// it, and who on that day could be named the acting manager.
    struct Left: Codable, Hashable {
        var date: String?
        var day: String?
        var from: String?
        var to: String?
        var minutes: Int?
        var reasons: [Reason] = []
        var couldAct: [String] = []

        struct Reason: Codable, Hashable {
            var employee: String?
            var why: String?
            init(from decoder: Decoder) throws {
                guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
                employee = c.sfText(.employee); why = c.sfText(.why)
            }
            enum CodingKeys: String, CodingKey { case employee, why }
            /// "Max — on approved time off".
            var line: String { [employee, why].compactMap { $0 }.joined(separator: " \u{2014} ") }
        }

        enum CodingKeys: String, CodingKey {
            case date, day, from, to, minutes, reasons
            case couldAct = "could_act"
        }

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            date = c.sfText(.date); day = c.sfText(.day); from = c.sfText(.from); to = c.sfText(.to)
            minutes = c.sfInt(.minutes)
            reasons = c.sfList(Reason.self, .reasons)
            couldAct = c.sfWords(.couldAct)
        }
    }

    struct Shortfall: Codable, Hashable {
        var text: String?
        var dates: [String] = []
        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            text = c.sfText(.text); dates = c.sfWords(.dates)
        }
        enum CodingKeys: String, CodingKey { case text, dates }
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        extended = c.sfInt(.extended); added = c.sfInt(.added)
        left = c.sfList(Left.self, .left)
        shortfall = (try? c.decodeIfPresent(Shortfall.self, forKey: .shortfall)) ?? nil
    }
    enum CodingKeys: String, CodingKey { case extended, added, left, shortfall }
}

// MARK: - Minimum hours (`min_hours`)

struct MinHoursReport: Codable, Hashable {
    var moved: Int?
    var added: Int?
    var left: [Left] = []

    struct Left: Codable, Hashable {
        var employee: String?
        var shortBy: Double?
        var min: Double?
        var reason: String?

        enum CodingKeys: String, CodingKey {
            case employee, min, reason
            case shortBy = "short_by"
        }

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            employee = c.sfText(.employee); shortBy = c.sfDouble(.shortBy); min = c.sfDouble(.min)
            reason = c.sfText(.reason)
        }

        /// "Cook — 1h under the 40h minimum you set: <reason>".
        var line: String {
            var s = employee ?? "Somebody"
            if let shortBy, let min {
                s += " \u{2014} \(CavnarQualityFormat.hours(shortBy))h under the \(CavnarQualityFormat.hours(min))h minimum you set"
            } else {
                s += " is under the minimum hours you set"
            }
            if let reason { s += ": \(reason)" }
            return s
        }
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        moved = c.sfInt(.moved); added = c.sfInt(.added); left = c.sfList(Left.self, .left)
    }
    enum CodingKeys: String, CodingKey { case moved, added, left }
}

// MARK: - What the generation could not do (P-34, E-20, E-30)

/// A date the generation could not write; the rest of the week is kept.
struct UnwrittenDate: Codable, Hashable {
    var date: String
    var day: String?
    var why: String?

    init(from decoder: Decoder) throws {
        if let s = try? decoder.singleValueContainer().decode(String.self) {
            date = s; return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let d = c.sfText(.date) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no date"))
        }
        date = d; day = c.sfText(.day); why = c.sfText(.why)
    }
    enum CodingKeys: String, CodingKey { case date, day, why }
}

/// A date nobody on the team can work (time off or availability).
struct UnstaffableDate: Codable, Hashable {
    var date: String
    var day: String?
    var reasons: [String] = []

    init(from decoder: Decoder) throws {
        if let s = try? decoder.singleValueContainer().decode(String.self) {
            date = s; return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let d = c.sfText(.date) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no date"))
        }
        date = d; day = c.sfText(.day)
        // {why: how many people} — "approved time off (3)" — or a plain list.
        if let byWhy = (try? c.decodeIfPresent([String: Int].self, forKey: .reasons)) ?? nil {
            reasons = byWhy.sorted { $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value }
                .map { "\($0.key) (\($0.value))" }
        } else {
            reasons = c.sfWords(.reasons)
        }
    }
    enum CodingKeys: String, CodingKey { case date, day, reasons }
}

/// A first week drafted with no history of its own.
struct StartingPoint: Codable, Hashable {
    var noHistory = false
    var floors: Bool?
    var borrowed: Bool?

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        noHistory = c.sfBool(.noHistory) ?? false
        floors = c.sfBool(.floors); borrowed = c.sfBool(.borrowed)
    }
    enum CodingKeys: String, CodingKey {
        case floors, borrowed
        case noHistory = "no_history"
    }
}

// MARK: - Shift requirements (E: P-19, D-23, D-25)

/// One date × daypart the week was written and scored to: each role's
/// number, why it moved off the usual crew, the staffing asks folded in.
struct RequirementRow: Codable, Hashable, Identifiable {
    var date: String?
    var day: String?
    var daypart: String?
    var roles: [Role] = []
    var reasons: [String] = []
    var leader: [String] = []
    var factor: Double?
    var window: [String] = []

    var id: String { "\(date ?? "")-\(daypart ?? "")" }
    var isLate: Bool { daypart == "late" }

    struct Role: Codable, Hashable {
        var role: String?
        var required: Int?
        var floor: Int?
        var typical: Int?
        var asked: Int?
        var reason: String?
        var borrowed: Bool?

        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            role = c.sfText(.role); required = c.sfInt(.required); floor = c.sfInt(.floor)
            typical = c.sfInt(.typical); asked = c.sfInt(.asked); reason = c.sfText(.reason)
            borrowed = c.sfBool(.borrowed)
        }
        enum CodingKeys: String, CodingKey { case role, required, floor, typical, asked, reason, borrowed }

        /// "3 Server (usual 2) · +1 asked" — the number, the usual crew when
        /// the date moved it, and the staffing asks folded in.
        var line: String {
            var s = "\(required ?? 0) \(role ?? "")"
            if let typical, let required, typical != required { s += " (usual \(typical))" }
            if let floor, floor > 0, floor >= (required ?? 0) { s += " \u{00B7} your floor" }
            if let asked, asked > 0 { s += " \u{00B7} +\(asked) asked" }
            return s
        }
    }

    enum CodingKeys: String, CodingKey { case date, day, daypart, roles, reasons, leader, factor, window }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        date = c.sfText(.date); day = c.sfText(.day); daypart = c.sfText(.daypart)
        roles = c.sfList(Role.self, .roles)
        reasons = c.sfWords(.reasons)
        leader = c.sfWords(.leader)
        factor = c.sfDouble(.factor)
        // The late row's window, as minutes past midnight or as times.
        if let mins = (try? c.decodeIfPresent([Int].self, forKey: .window)) ?? nil {
            window = mins.map { LaborViewModel.shiftTimeText(minutes: $0) }
        } else {
            window = c.sfWords(.window)
        }
    }

    /// "Lunch", "Dinner" or "Late night 10:00pm–2:00am".
    var partLabel: String {
        switch daypart {
        case "morning": return "Lunch"
        case "night": return "Dinner"
        case "late":
            return window.count == 2 ? "Late night \(window[0])\u{2013}\(window[1])" : "Late night"
        default: return (daypart ?? "").capitalized
        }
    }
}

// MARK: - Shift Quality additions (D1a, D1b)

/// What holds a shift at its number (SQ-8): the capping dimension, the hard
/// rules, or a daypart nobody was written onto — `text` says it.
struct QualityHeldBy: Codable, Hashable {
    var key: String?
    var label: String?
    var score: Int?
    var floor: Double?
    var text: String?

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        key = c.sfText(.key); label = c.sfText(.label); score = c.sfInt(.score); floor = c.sfDouble(.floor)
        text = c.sfText(.text)
    }
    enum CodingKeys: String, CodingKey { case key, label, score, floor, text }
}

/// One deduction from the read's completeness, with the points it cost.
struct QualityConfidenceDeduction: Codable, Hashable {
    var reason: String
    var points: Double?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let r = c.sfText(.reason) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no reason"))
        }
        reason = r; points = c.sfDouble(.points)
    }
    enum CodingKeys: String, CodingKey { case reason, points }
}

extension AnyCodableValue {
    var objectValue: [String: AnyCodableValue]? { if case .object(let o) = self { return o }; return nil }
    var arrayValue: [AnyCodableValue]? { if case .array(let a) = self { return a }; return nil }
    var stringValue: String? {
        switch self {
        case .string(let s): return s
        case .int(let i): return String(i)
        case .double(let d): return d == d.rounded() ? String(Int(d)) : String(format: "%.1f", d)
        default: return nil
        }
    }
    var doubleValue: Double? {
        switch self {
        case .int(let i): return Double(i)
        case .double(let d): return d
        case .string(let s): return Double(s)
        default: return nil
        }
    }
    var boolValue: Bool? {
        switch self {
        case .bool(let b): return b
        case .int(let i): return i != 0
        default: return nil
        }
    }
    /// A list of names: strings, or objects carrying a `name`.
    var names: [String] {
        (arrayValue ?? []).compactMap { $0.stringValue ?? $0.objectValue?["name"]?.stringValue }
    }
}

// MARK: - Review extras (schedule_rules.summarize + the job's review keys)

/// A breach about a day — no manager on, a floor short, nobody at close —
/// shown on that day's header, never on an innocent person's row (E-13).
struct ReviewHardDay: Codable, Hashable {
    var date: String?
    var day: String?
    var kind: String?
    var detail: String?

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        date = c.sfText(.date); day = c.sfText(.day); kind = c.sfText(.kind); detail = c.sfText(.detail)
    }
    enum CodingKeys: String, CodingKey { case date, day, kind, detail }
}

/// A repair stage that did not run (P-3, P-17). A blocking one holds the
/// publish until the owner has read it.
struct ReviewStageFailure: Codable, Hashable {
    var stage: String?
    var blocksPublish = false
    var line: String?

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        stage = c.sfText(.stage); blocksPublish = c.sfBool(.blocksPublish) ?? false; line = c.sfText(.line)
    }
    enum CodingKeys: String, CodingKey {
        case stage, line
        case blocksPublish = "blocks_publish"
    }

    /// The owner-facing sentence without its "⚠" marker.
    var text: String? {
        guard let line else { return nil }
        let t = line.hasPrefix("⚠") ? String(line.dropFirst()).trimmingCharacters(in: .whitespaces) : line
        return t.isEmpty ? nil : t
    }
}

/// Something the finished week does not meet (PR-11, `review.unmet`).
struct ReviewUnmetItem: Codable, Hashable {
    var kind: String?
    var date: String?
    var day: String?
    var daypart: String?
    var what: String?
    var why: String?

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        kind = c.sfText(.kind); date = c.sfText(.date); day = c.sfText(.day); daypart = c.sfText(.daypart)
        what = c.sfText(.what); why = c.sfText(.why)
    }
    enum CodingKeys: String, CodingKey { case kind, date, day, daypart, what, why }

    /// How the item reads: a hard rule (ember), a staffing miss (orange),
    /// a number to know (neutral) or a rule nobody's code can check.
    enum Style { case hard, staffing, neutral, unchecked }

    var style: Style {
        switch kind ?? "" {
        case "manager", "floor", "owner_rule", "closer", "close", "role_close": return .hard
        case "coverage", "leadership", "strength", "station", "ask": return .staffing
        case "unchecked_rule": return .unchecked
        default: return .neutral
        }
    }

    /// "lunch/day" or "dinner/night", the owner's words for a daypart.
    var partWords: String? {
        switch daypart ?? "" {
        case "morning", "lunch", "day": return "lunch/day"
        case "night", "dinner": return "dinner/night"
        case "late": return "late night"
        default: return nil
        }
    }
}

/// The setup a week was drafted against, for the owner to confirm (F1-9):
/// `leader_rules_inactive` can be adopted, `close_times_missing` goes to Hours.
struct ReviewSetupItem: Codable, Hashable {
    var kind: String?
    var text: String?
    var canAdopt = false
    var days: [String] = []

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        kind = c.sfText(.kind); text = c.sfText(.text); canAdopt = c.sfBool(.canAdopt) ?? false
        days = c.sfWords(.days)
    }
    enum CodingKeys: String, CodingKey {
        case kind, text, days
        case canAdopt = "can_adopt"
    }
}

/// A fact on file under a name nobody on the roster goes by (D-8).
struct ReviewUnmatchedName: Codable, Hashable {
    var source: String?
    var name: String?
    var detail: String?
    var suggestion: String?

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        source = c.sfText(.source); name = c.sfText(.name); detail = c.sfText(.detail)
        suggestion = c.sfText(.suggestion)
    }
    enum CodingKeys: String, CodingKey { case source, name, detail, suggestion }
}

/// Why the budget trim stopped above the budget (SQ-7): how many shifts
/// each reason held, and a few examples.
struct ReviewBudgetConflict: Codable, Hashable {
    var held: [String: Int] = [:]
    var examples: [Example] = []

    struct Example: Codable, Hashable {
        var reason: String?
        var date: String?
        var day: String?
        var label: String?
        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            reason = c.sfText(.reason); date = c.sfText(.date); day = c.sfText(.day); label = c.sfText(.label)
        }
        enum CodingKeys: String, CodingKey { case reason, date, day, label }
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        held = ((try? c.decodeIfPresent([String: Int].self, forKey: .held)) ?? nil) ?? [:]
        examples = c.sfList(Example.self, .examples)
    }
    enum CodingKeys: String, CodingKey { case held, examples }

    /// The owner's words for each reason the trim kept hours.
    static let reasonWords: [String: String] = [
        "requirement": "at the shift's requirement", "learned": "kept by what the draft learned",
        "troubled": "on a daypart that has gone wrong before", "rule": "a rule a cut would break",
        "floor": "at your floor", "last_of_role": "the last of their role on",
        "station": "covering a station", "min_hours": "keeping somebody at their minimum hours",
        "only_shift": "somebody's only shift", "kept": "on a day you kept", "review": "flagged for review",
    ]

    /// "6 at the shift's requirement · 2 at your floor" — biggest first.
    var heldLine: String? {
        let parts = held.filter { $0.value > 0 }.sorted { $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value }
            .map { "\($0.value) \(Self.reasonWords[$0.key] ?? $0.key.replacingOccurrences(of: "_", with: " "))" }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }
}

/// A night left over the section count because a cut would have gone
/// under a floor or broken a rule (P-29).
struct ReviewCapConflict: Codable, Hashable {
    var date: String?
    var day: String?
    var at: String?
    var on: Int?
    var cap: Int?

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        date = c.sfText(.date); day = c.sfText(.day); at = c.sfText(.at); on = c.sfInt(.on); cap = c.sfInt(.cap)
    }
    enum CodingKeys: String, CodingKey { case date, day, at, on, cap }

    var line: String {
        var s = CavnarDate.dayDate(day, date)
        if let at { s += " at \(at)" }
        if let on, let cap { s += ": \(on) on the floor against \(cap) sections" }
        return s
    }
}

/// A Cavnar AI change's row taken out by apply fixes or Improve — returned
/// as the next save's `cavnar_changes` so the save credits Cavnar AI, not
/// the manager's habit (L-5).
struct CavnarRemovedRow: Codable, Hashable {
    var date: String?
    var employee: String?
    var shiftStart: String?
    var source: String?

    enum CodingKeys: String, CodingKey {
        case date, employee, source
        case shiftStart = "shift_start"
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        date = c.sfText(.date); employee = c.sfText(.employee); shiftStart = c.sfText(.shiftStart)
        source = c.sfText(.source)
    }
}

/// A fix whose row was edited away (`index: null`) still names its shift.
struct ReviewFixRow: Codable, Hashable {
    var date: String?
    var day: String?
    var employee: String?
    var shiftStart: String?
    var shiftEnd: String?

    enum CodingKeys: String, CodingKey {
        case date, day, employee
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        date = c.sfText(.date); day = c.sfText(.day); employee = c.sfText(.employee)
        shiftStart = c.sfText(.shiftStart); shiftEnd = c.sfText(.shiftEnd)
    }

    /// "Ana · Tuesday 10/6/26 · 4:00pm–10:00pm".
    var label: String {
        let day = self.day ?? date.flatMap { LaborViewModel.weekdayName($0) }
        let when = [shiftStart, shiftEnd].compactMap { $0 }.joined(separator: "\u{2013}")
        return [employee, CavnarDate.dayDate(day, date), when].compactMap { $0 }.filter { !$0.isEmpty }
            .joined(separator: " \u{00B7} ")
    }
}

// MARK: - Save: the one-tap "why" (L-35, H1-1)

struct EditWhyQuestion: Codable, Hashable, Identifiable {
    var key: String
    var kind: String?
    var text: String?
    var options: [Option] = []

    var id: String { key }

    struct Option: Codable, Hashable {
        var answer: String
        var label: String
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let a = c.sfText(.answer) else {
                throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no answer"))
            }
            answer = a; label = c.sfText(.label) ?? a
        }
        enum CodingKeys: String, CodingKey { case answer, label }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let k = c.sfText(.key) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no key"))
        }
        key = k; kind = c.sfText(.kind); text = c.sfText(.text); options = c.sfList(Option.self, .options)
    }
    enum CodingKeys: String, CodingKey { case key, kind, text, options }
}

// MARK: - Publish check (G-1, H1-6)

struct PublishNote: Codable, Hashable {
    var key: String?
    var text: String
    init(from decoder: Decoder) throws {
        if let s = try? decoder.singleValueContainer().decode(String.self) { text = s; return }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let t = c.sfText(.text) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no text"))
        }
        key = c.sfText(.key); text = t
    }
    enum CodingKeys: String, CodingKey { case key, text }
}

/// The week's hours split by pay (E-7): the hourly part is what the hourly
/// budget is held to; the salaried part is never spent from it.
struct PublishHours: Codable, Hashable {
    var hourly: Double?
    var salaried: Double?
    var total: Double?
    var budget: Double?

    init(hourly: Double?, salaried: Double?, total: Double? = nil, budget: Double? = nil) {
        self.hourly = hourly; self.salaried = salaried; self.total = total; self.budget = budget
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        hourly = c.sfDouble(.hourly); salaried = c.sfDouble(.salaried); total = c.sfDouble(.total)
        budget = c.sfDouble(.budget)
    }
    enum CodingKeys: String, CodingKey { case hourly, salaried, total, budget }

    /// "312h hourly of 320h budget · 110h salaried" — each part only when known.
    func line(budget fallback: Double? = nil) -> String? {
        guard let hourly else { return nil }
        var s = "\(CavnarQualityFormat.hours(hourly))h hourly"
        if let b = budget ?? fallback, b > 0 { s += " of \(CavnarQualityFormat.hours(b))h budget" }
        if let salaried, salaried > 0 { s += " \u{00B7} \(CavnarQualityFormat.hours(salaried))h salaried" }
        return s
    }
}

/// The rows the manager's own record says they are likely to change (L-15),
/// shown only once the predictor's backtest is good enough (`ready`).
struct LikelyToChange: Codable, Hashable {
    var ready = false
    var reason: String?
    var note: String?
    var rows: [Row] = []

    struct Row: Codable, Hashable {
        var employee: String?
        var date: String?
        var role: String?
        var shiftStart: String?
        var likelihood: Double?
        var text: String?
        enum CodingKeys: String, CodingKey {
            case employee, date, role, likelihood, text
            case shiftStart = "shift_start"
        }
        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            employee = c.sfText(.employee); date = c.sfText(.date); role = c.sfText(.role)
            shiftStart = c.sfText(.shiftStart); likelihood = c.sfDouble(.likelihood); text = c.sfText(.text)
        }
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        ready = c.sfBool(.ready) ?? false; reason = c.sfText(.reason); note = c.sfText(.note)
        rows = c.sfList(Row.self, .rows)
    }
    enum CodingKeys: String, CodingKey { case ready, reason, note, rows }
}

// MARK: - Sections (GET labor/schedule/sections, H2-2)

struct ScheduleSections: Decodable {
    var sections: [String] = []
    var assigned: [Assigned] = []
    var usual: [Usual] = []
    var fohRoles: [String] = []

    struct Assigned: Codable, Hashable {
        var date: String?
        var employee: String?
        var shiftStart: String?
        var section: String?
        enum CodingKeys: String, CodingKey {
            case date, employee, section
            case shiftStart = "shift_start"
        }
        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            date = c.sfText(.date); employee = c.sfText(.employee); shiftStart = c.sfText(.shiftStart)
            section = c.sfText(.section)
        }
    }

    /// A server's usual section by weekday and daypart, from the memory.
    struct Usual: Codable, Hashable {
        var employee: String?
        var day: String?
        var daypart: String?
        var section: String?
        init(from decoder: Decoder) throws {
            guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
            employee = c.sfText(.employee); day = c.sfText(.day); daypart = c.sfText(.daypart)
            section = c.sfText(.section)
        }
        enum CodingKeys: String, CodingKey { case employee, day, daypart, section }
    }

    enum CodingKeys: String, CodingKey {
        case sections, assigned, usual
        case fohRoles = "foh_roles"
    }

    init() {}

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        sections = c.sfWords(.sections)
        assigned = c.sfList(Assigned.self, .assigned)
        usual = c.sfList(Usual.self, .usual)
        fohRoles = c.sfWords(.fohRoles).map { $0.lowercased() }
    }

    /// "HH:MM" from "4:00pm" — the key the section store uses for a shift's start.
    static func clock24(_ start: String?) -> String? {
        guard let m = LaborViewModel.shiftMinutes(start) else { return nil }
        return String(format: "%02d:%02d", m / 60, m % 60)
    }

    func section(for row: ScheduleRow) -> String? {
        let who = (row.employee ?? "").lowercased()
        let start = Self.clock24(row.shiftStart)
        return assigned.first {
            $0.date == row.date && ($0.employee ?? "").lowercased() == who
                && ($0.shiftStart == start || $0.shiftStart == row.shiftStart)
        }?.section
    }

    func usual(for row: ScheduleRow) -> Usual? {
        let who = (row.employee ?? "").lowercased()
        let day = (row.day ?? row.date.flatMap { LaborViewModel.weekdayName($0) } ?? "").lowercased()
        let part = GeneratedSchedule.daypart(of: row.shiftStart)
        return usual.first {
            ($0.employee ?? "").lowercased() == who && ($0.day ?? "").lowercased() == day
                && ($0.daypart ?? part) == part && $0.section != nil
        }
    }

    func isFrontOfHouse(_ role: String?) -> Bool {
        let r = (role ?? "").lowercased()
        guard !r.isEmpty else { return false }
        return (fohRoles.isEmpty ? ["server"] : fohRoles).contains { r.contains($0) }
    }
}

// MARK: - What the schedule has learned (GET labor/schedule-memory, H2-1)

struct ScheduleMemoryItem: Codable, Hashable, Identifiable {
    var key: String
    var kind: String?
    var classLabel: String?
    var text: String?
    var status: String?
    var statusLabel: String?
    var heldInCode = false
    var boundBy: String?
    var confidencePct: String?
    var opportunities: Int?
    var hits: Int?
    var lastConfirmedByHand: String?
    var retiredWords: String?
    var canKeep = false
    var canLetGo = false
    var canBeRule = false

    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, kind, text, status, hits, opportunities
        case classLabel = "class_label"
        case statusLabel = "status_label"
        case heldInCode = "held_in_code"
        case boundBy = "bound_by"
        case confidencePct = "confidence_pct"
        case lastConfirmedByHand = "last_confirmed_by_hand"
        case retiredWords = "retired_words"
        case canKeep = "can_keep"
        case canLetGo = "can_let_go"
        case canBeRule = "can_be_rule"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let k = c.sfText(.key) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no key"))
        }
        key = k
        kind = c.sfText(.kind); classLabel = c.sfText(.classLabel); text = c.sfText(.text)
        status = c.sfText(.status); statusLabel = c.sfText(.statusLabel)
        heldInCode = c.sfBool(.heldInCode) ?? false; boundBy = c.sfText(.boundBy)
        // "—" below the sample floor; a number arrives as "72%" or 72.
        if let pct = c.sfText(.confidencePct) {
            confidencePct = pct == "\u{2014}" || pct.hasSuffix("%") ? pct : pct + "%"
        }
        opportunities = c.sfInt(.opportunities); hits = c.sfInt(.hits)
        lastConfirmedByHand = c.sfText(.lastConfirmedByHand); retiredWords = c.sfText(.retiredWords)
        canKeep = c.sfBool(.canKeep) ?? false; canLetGo = c.sfBool(.canLetGo) ?? false
        canBeRule = c.sfBool(.canBeRule) ?? false
    }

    /// "2 of 3 weeks" from hits over opportunities.
    var evidence: String? {
        guard let opportunities, opportunities > 0 else { return nil }
        return "\(hits ?? 0) of \(opportunities)"
    }

    /// Where the fact already acts, in the owner's words — the server names
    /// the code that holds it, which is never shown as it stands.
    var boundWords: String? {
        guard let b = boundBy?.lowercased() else { return nil }
        if b.contains("requirement") { return "Already in the requirements" }
        if b.contains("reliability") { return "Weighed as reliability" }
        if b.contains("preference") { return "Weighed as a preference" }
        return "Already applied elsewhere"
    }
}

struct ScheduleMemoryView: Decodable {
    var items: [ScheduleMemoryItem] = []
    var consolidatedAt: String?
    var canAnswer = false
    var canMakeRules = false

    enum CodingKeys: String, CodingKey {
        case items
        case consolidatedAt = "consolidated_at"
        case canAnswer = "can_answer"
        case canMakeRules = "can_make_rules"
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        items = c.sfList(ScheduleMemoryItem.self, .items)
        consolidatedAt = c.sfText(.consolidatedAt)
        canAnswer = c.sfBool(.canAnswer) ?? false
        canMakeRules = c.sfBool(.canMakeRules) ?? false
    }
}

// MARK: - Measured ratings (GET labor/ratings/suggested, H2-3)

struct SuggestedRating: Codable, Hashable, Identifiable {
    var name: String
    var tickets: Int?
    var suggested: Int?
    var current: Int?
    var differs = false
    var reason: String?

    var id: String { name }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let n = c.sfText(.name) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no name"))
        }
        name = n; tickets = c.sfInt(.tickets); suggested = c.sfInt(.suggested); current = c.sfInt(.current)
        differs = c.sfBool(.differs) ?? (current != suggested); reason = c.sfText(.reason)
    }
    enum CodingKeys: String, CodingKey { case name, tickets, suggested, current, differs, reason }
}

struct SuggestedRatings: Decodable {
    var available = false
    var servers: [SuggestedRating] = []
    var note: String?
    var reason: String?
    var minTickets: Int?

    enum CodingKeys: String, CodingKey {
        case available, servers, note, reason
        case minTickets = "min_tickets"
    }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else { return }
        available = c.sfBool(.available) ?? false
        servers = c.sfList(SuggestedRating.self, .servers)
        note = c.sfText(.note); reason = c.sfText(.reason); minTickets = c.sfInt(.minTickets)
    }
}

// MARK: - The draft's banners, read once from the payload

extension GeneratedSchedule {
    /// The draft's reviewed manager plan: the generation's own, else what a
    /// reopened week kept in its review.
    var plan: ManagerPlan? { managerPlan ?? review?.managerPlan }

    var coverage: ManagerCoverage? { managerCoverage ?? review?.managerCoverage }

    var minHoursLeft: [MinHoursReport.Left] { (minHours ?? review?.minHours)?.left ?? [] }

    var unwritten: [UnwrittenDate] { unwrittenDates?.items ?? review?.unwrittenDates?.items ?? [] }

    var unstaffable: [UnstaffableDate] { unstaffableDates?.items ?? review?.unstaffableDates?.items ?? [] }

    /// The week's hours split by pay, when the generation said it.
    var hoursSplit: PublishHours? {
        guard hoursHourly != nil else { return nil }
        return PublishHours(hourly: hoursHourly, salaried: hoursSalaried, total: hoursScheduled, budget: hoursBudget)
    }

    /// "Saturday 10/10/26 and Sunday 10/11/26 weren't written — <why>. The rest of the week is here."
    var partialLine: String? {
        let days = unwritten
        guard !days.isEmpty else { return nil }
        let names = days.map { CavnarDate.dayDate($0.day ?? LaborViewModel.weekdayName($0.date), $0.date) }
        let whys = Array(Set(days.compactMap(\.why))).sorted()
        var s = "\(CavnarDate.dayList(names)) \(days.count == 1 ? "wasn\u{2019}t" : "weren\u{2019}t") written"
        if let why = whys.first { s += " \u{2014} \(why)" }
        return s + ". The rest of the week is here."
    }

    /// "Nobody on the team can work Saturday 10/10/26 (time off or availability)".
    var unstaffableLine: String? {
        let days = unstaffable
        guard !days.isEmpty else { return nil }
        let names = days.map { CavnarDate.dayDate($0.day ?? LaborViewModel.weekdayName($0.date), $0.date) }
        return "Nobody on the team can work \(CavnarDate.dayList(names)) (time off or availability). "
            + "Mark \(days.count == 1 ? "it" : "them") closed or fix availability, then redo "
            + "\(days.count == 1 ? "that day" : "those days")."
    }
}

