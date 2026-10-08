import Foundation

// iOS parity, Labor & the AI schedule (10/7/26). The payloads the phone
// reads for the honest generation copy (#15), a run in the way (409), the
// open-shift board (#25), tonight's covers (#69), the week's build notes and
// the rules made from them (#46), a staff note held as a constraint (#68)
// and the automation that drafts the week (in the Generate card).

// MARK: - How long a draft takes (#15)

/// `typical` on a generate answer (schedule_engine.typical_generation_seconds):
/// the median of this restaurant's recent whole-week drafts, else every
/// restaurant's — measured, never guessed. Nil until two have run.
struct GenerationTypical: Codable, Equatable {
    let seconds: Int
    let n: Int?
    /// "yours" | "all"
    let basis: String?
}

enum GenerationCopy {
    /// The step clock's stretch: the steps are a 75-second sketch, and a
    /// week now takes minutes — stretched to the measured typical draft, or
    /// three times when nothing is measured (the web's scheduleTiming).
    static func stepScale(typical: GenerationTypical?) -> Double {
        guard let t = typical, t.seconds > 0 else { return 3 }
        return max(1, Double(t.seconds) / 75)
    }

    /// "3:12 so far. A full week usually takes about 6 min (your last 4
    /// drafts). Taking longer than usual — it is still working, and stops by
    /// 6:42pm at the latest. You can leave — the draft lands in History."
    /// The web's renderScheduleEta, word for word where the phone allows.
    static func etaLine(elapsed: TimeInterval, typical: GenerationTypical?, until: Date?,
                        clock: (Date) -> String = { CavnarDate.time($0) }) -> String {
        let secs = max(0, Int(elapsed.rounded()))
        var out = "\(secs / 60):\(String(format: "%02d", secs % 60)) so far. "
        if let t = typical, t.seconds > 0 {
            let mins = max(1, Int((Double(t.seconds) / 60).rounded()))
            let n = t.n ?? 0
            let basis = t.basis == "yours" ? "your last \(n) drafts" : "the last \(n) drafts on Cavnar AI"
            out += "A full week usually takes about \(mins) min (\(basis))."
        } else {
            out += "Cavnar AI works through the whole week before it writes it, so a draft takes several minutes."
        }
        let slow = typical.map { $0.seconds > 0 && Double(secs) > Double($0.seconds) * 1.5 } ?? (secs > 600)
        if slow {
            out += " Taking longer than usual \u{2014} it is still working"
                + (until.map { ", and stops by \(clock($0)) at the latest" } ?? "") + "."
        }
        return out + " You can leave \u{2014} the draft lands in History."
    }
}

/// A 409 from generate-schedule: another week is being built. `running` is
/// what that run was asked; `job_id` lets the phone follow it.
struct GenerationBusy: Decodable, Equatable {
    struct Running: Decodable, Equatable {
        var weekStart: String? = nil
        var dates: [String]? = nil
        var historyId: Int? = nil
        enum CodingKeys: String, CodingKey {
            case dates
            case weekStart = "week_start"
            case historyId = "history_id"
        }
    }
    var busy: Bool? = nil
    var running: Running? = nil
    var jobId: String? = nil
    var waitSeconds: Int? = nil
    var typical: GenerationTypical? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case busy, running, typical, error
        case jobId = "job_id"
        case waitSeconds = "wait_seconds"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        busy = try? c.decodeIfPresent(Bool.self, forKey: .busy)
        running = try? c.decodeIfPresent(Running.self, forKey: .running)
        jobId = try? c.decodeIfPresent(String.self, forKey: .jobId)
        waitSeconds = try? c.decodeIfPresent(Int.self, forKey: .waitSeconds)
        typical = try? c.decodeIfPresent(GenerationTypical.self, forKey: .typical)
        error = try? c.decodeIfPresent(String.self, forKey: .error)
    }

    /// "The week of 10/12/26 is being built" / "Redoing Fri 10/16/26 of the
    /// week of 10/12/26" — the card's headline; the server's own sentence
    /// is the detail.
    var headline: String {
        let week = running?.weekStart.map { CavnarDate.mdy($0) }
        let days = (running?.dates ?? []).filter { !$0.isEmpty }
        if !days.isEmpty {
            let named = days.map { (Self.weekday($0).map { $0 + " " } ?? "") + CavnarDate.mdy($0) }
                .joined(separator: ", ")
            return "Cavnar AI is redoing \(named)" + (week.map { " of the week of \($0)" } ?? "")
        }
        return week.map { "The week of \($0) is being built" } ?? "Another schedule is being built"
    }

    /// "Fri" for an ISO date, off any actor.
    static func weekday(_ iso: String) -> String? {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: String(iso.prefix(10))) else { return nil }
        f.dateFormat = "EEE"
        return f.string(from: d)
    }
}

// MARK: - Open shifts (#25)

/// POST /labor/open-shifts — {date, shift_start, shift_end?, role?,
/// employee?, offer_to?, note?}: every key strategy_routes._do_open_shift_post
/// reads. Blank optional fields are left out, as the web's form sends "".
struct OpenShiftPostBody: Encodable, Equatable {
    let date: String
    let shiftStart: String
    var shiftEnd: String? = nil
    var role: String? = nil
    var employee: String? = nil
    var offerTo: String? = nil
    var note: String? = nil

    enum CodingKeys: String, CodingKey {
        case date, role, employee, note
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case offerTo = "offer_to"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(date, forKey: .date)
        try c.encode(shiftStart, forKey: .shiftStart)
        func put(_ v: String?, _ k: CodingKeys) throws {
            if let v = v?.trimmingCharacters(in: .whitespacesAndNewlines), !v.isEmpty { try c.encode(v, forKey: k) }
        }
        try put(shiftEnd, .shiftEnd)
        try put(role, .role)
        try put(employee, .employee)
        try put(offerTo, .offerTo)
        try put(note, .note)
    }
}

/// An offer of an open shift to one named person, still waiting on them
/// (`offers` on GET /labor/shift-requests).
struct ShiftOffer: Decodable, Identifiable, Equatable {
    let id: Int
    let requestId: Int
    let name: String
    enum CodingKeys: String, CodingKey {
        case id, name
        case requestId = "request_id"
    }
}

// MARK: - Covers (#69)

struct CoversDay: Decodable, Identifiable, Equatable {
    let date: String
    let covers: Int
    let source: String?
    var id: String { date }
}

struct CoversPayload: Decodable {
    var ok: Bool = false
    var days: [CoversDay] = []
    var error: String? = nil
    enum CodingKeys: String, CodingKey { case ok, days, error }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? false
        days = (try? c.decodeIfPresent([CoversDay].self, forKey: .days)) ?? []
        error = try? c.decodeIfPresent(String.self, forKey: .error)
    }

    /// "about 84 a day" over the nights on file; nil with none.
    var averageLine: String? {
        guard !days.isEmpty else { return nil }
        let avg = Double(days.map(\.covers).reduce(0, +)) / Double(days.count)
        return "\(days.count) \(days.count == 1 ? "day" : "days") on file \u{00B7} about \(Int(avg.rounded())) a day"
    }
}

/// POST /labor/covers {rows: [{date, covers}]} — the web's one-night save.
struct CoversSaveBody: Encodable, Equatable {
    struct Row: Encodable, Equatable { let date: String; let covers: Int }
    let rows: [Row]
}

struct CoversSaveResponse: Decodable {
    let ok: Bool
    let written: Int?
    let errors: [String]?
    let error: String?
}

// MARK: - How you like the week built (#46)

/// GET /account/targets — the two fields the Generate card reads and
/// writes: the restaurant's labor target and its build notes.
struct BuildTargets: Decodable, Equatable {
    var laborTargetPct: Double? = nil
    var schedNotes: String? = nil
    enum CodingKeys: String, CodingKey {
        case laborTargetPct = "labor_target_pct"
        case schedNotes = "sched_notes"
    }
    init(laborTargetPct: Double? = nil, schedNotes: String? = nil) {
        self.laborTargetPct = laborTargetPct
        self.schedNotes = schedNotes
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        laborTargetPct = (try? c.decodeIfPresent(Double.self, forKey: .laborTargetPct)) ?? nil
        schedNotes = (try? c.decodeIfPresent(String.self, forKey: .schedNotes)) ?? nil
    }
}

struct BuildTargetsResponse: Decodable {
    var ok = false
    var targets: BuildTargets? = nil
    var canEdit = false
    var error: String? = nil
    enum CodingKeys: String, CodingKey {
        case ok, targets, error
        case canEdit = "can_edit"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? false
        targets = try? c.decodeIfPresent(BuildTargets.self, forKey: .targets)
        canEdit = (try? c.decodeIfPresent(Bool.self, forKey: .canEdit)) ?? false
        error = try? c.decodeIfPresent(String.self, forKey: .error)
    }
}

/// One field per save, as the web's Studio sends it.
struct LaborTargetBody: Encodable, Equatable {
    let laborTargetPct: Double
    enum CodingKeys: String, CodingKey { case laborTargetPct = "labor_target_pct" }
}

struct SchedNotesBody: Encodable, Equatable {
    let schedNotes: String
    enum CodingKeys: String, CodingKey { case schedNotes = "sched_notes" }
}

/// What Cavnar AI can hold a sentence of the notes to.
struct NoteRuleDraft: Codable, Equatable {
    var role: String? = nil
    var roleChoices: [String]? = nil
    var min: Int? = nil
    var dayparts: [String]? = nil
    var days: [String]? = nil
    var scope: String? = nil
    enum CodingKeys: String, CodingKey {
        case role, min, dayparts, days, scope
        case roleChoices = "role_choices"
    }
}

/// One sentence of the notes as Cavnar AI reads it (schedule_note_rules.
/// read_notes): a rule it can hold, one it can't, about one person, or
/// guidance — with the rule made from it when there is one.
struct NoteSentence: Decodable, Identifiable, Equatable {
    var id: Int = 0
    var text: String = ""
    var kind: String? = nil
    var why: String? = nil
    var rule: NoteRuleDraft? = nil
    var ruleId: Int? = nil
    var enforced: String? = nil
    var stale: String? = nil

    enum CodingKeys: String, CodingKey {
        case text, kind, why, rule, enforced, stale
        case ruleId = "rule_id"
    }
    init(text: String, kind: String?) { self.text = text; self.kind = kind }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = (try? c.decodeIfPresent(String.self, forKey: .text)) ?? ""
        kind = try? c.decodeIfPresent(String.self, forKey: .kind)
        why = try? c.decodeIfPresent(String.self, forKey: .why)
        rule = try? c.decodeIfPresent(NoteRuleDraft.self, forKey: .rule)
        ruleId = try? c.decodeIfPresent(Int.self, forKey: .ruleId)
        enforced = try? c.decodeIfPresent(String.self, forKey: .enforced)
        stale = try? c.decodeIfPresent(String.self, forKey: .stale)
    }
}

struct NoteRule: Decodable, Identifiable, Equatable {
    let id: Int
    let words: String
    var stale: String? = nil
    var sourceText: String? = nil
    enum CodingKeys: String, CodingKey {
        case id, words, stale
        case sourceText = "source_text"
    }
}

struct NoteRulesPayload: Decodable {
    var ok = false
    var weekOf: String? = nil
    var sentences: [NoteSentence] = []
    var rules: [NoteRule] = []
    var roles: [String] = []
    var canEdit = false
    var error: String? = nil
    enum CodingKeys: String, CodingKey {
        case ok, sentences, rules, roles, error
        case weekOf = "week_of"
        case canEdit = "can_edit"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? false
        weekOf = try? c.decodeIfPresent(String.self, forKey: .weekOf)
        var said = (try? c.decodeIfPresent([NoteSentence].self, forKey: .sentences)) ?? []
        for i in said.indices { said[i].id = i }
        sentences = said
        rules = (try? c.decodeIfPresent([NoteRule].self, forKey: .rules)) ?? []
        roles = (try? c.decodeIfPresent([String].self, forKey: .roles)) ?? []
        canEdit = (try? c.decodeIfPresent(Bool.self, forKey: .canEdit)) ?? false
        error = try? c.decodeIfPresent(String.self, forKey: .error)
    }

    /// Rules made from earlier notes no sentence carries now.
    var earlierRules: [NoteRule] {
        let used = Set(sentences.compactMap(\.ruleId))
        return rules.filter { !used.contains($0.id) }
    }
}

/// POST /labor/note-rules — {role, min, dayparts, days, scope, week_start,
/// source_text}: every key strategy_routes._do_note_rule_add reads.
struct NoteRuleAddBody: Encodable, Equatable {
    let role: String
    let min: Int
    let dayparts: [String]
    let days: [String]
    let scope: String
    let weekStart: String
    let sourceText: String
    enum CodingKeys: String, CodingKey {
        case role, min, dayparts, days, scope
        case weekStart = "week_start"
        case sourceText = "source_text"
    }
}

// MARK: - A staff note held as a constraint (#68)

/// POST /labor/staff-note-holds — {employee_name, part_text, days?,
/// dayparts?, start?, end?}, as the web's "Hold it" posts the reading.
struct StaffNoteHoldBody: Encodable, Equatable {
    let employeeName: String
    let partText: String
    var days: [String]? = nil
    var dayparts: [String]? = nil
    var start: String? = nil
    var end: String? = nil
    enum CodingKeys: String, CodingKey {
        case days, dayparts, start, end
        case employeeName = "employee_name"
        case partText = "part_text"
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(employeeName, forKey: .employeeName)
        try c.encode(partText, forKey: .partText)
        if let days, !days.isEmpty { try c.encode(days, forKey: .days) }
        if let dayparts, !dayparts.isEmpty { try c.encode(dayparts, forKey: .dayparts) }
        try c.encodeIfPresent(start, forKey: .start)
        try c.encodeIfPresent(end, forKey: .end)
    }
}

// MARK: - The automation that drafts the week (in the Generate card)

struct ScheduleAutoDraft: Decodable, Equatable {
    var enabled = false
    var weekday = 3
    var day = "Thursday"
    var publishDay: String? = nil
    var externalTool = ""
    enum CodingKeys: String, CodingKey {
        case enabled, weekday, day
        case publishDay = "publish_day"
        case externalTool = "external_tool"
    }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = (try? c.decodeIfPresent(Bool.self, forKey: .enabled)) ?? false
        weekday = (try? c.decodeIfPresent(Int.self, forKey: .weekday)) ?? 3
        day = (try? c.decodeIfPresent(String.self, forKey: .day)) ?? "Thursday"
        publishDay = try? c.decodeIfPresent(String.self, forKey: .publishDay)
        externalTool = ((try? c.decodeIfPresent(String.self, forKey: .externalTool)) ?? nil) ?? ""
    }
}

struct ScheduleAutoPublish: Decodable, Equatable {
    var enabled = false
    var day: String? = nil
    var trust = 0
    var needed = 0
    var armed = false
    enum CodingKeys: String, CodingKey { case enabled, day, trust, needed, armed }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = (try? c.decodeIfPresent(Bool.self, forKey: .enabled)) ?? false
        day = try? c.decodeIfPresent(String.self, forKey: .day)
        trust = (try? c.decodeIfPresent(Int.self, forKey: .trust)) ?? 0
        needed = (try? c.decodeIfPresent(Int.self, forKey: .needed)) ?? 0
        armed = (try? c.decodeIfPresent(Bool.self, forKey: .armed)) ?? false
    }
}

// MARK: - The forecast behind a reopened week (#47)

/// A number that may arrive as a number, a numeric string or anything else
/// (nil) — never a failed decode of the week it rides in.
struct LenientDouble: Codable, Hashable {
    let value: Double?
    init(_ value: Double?) { self.value = value }
    init(from decoder: Decoder) throws {
        let c = try? decoder.singleValueContainer()
        if let d = try? c?.decode(Double.self) {
            value = d.isFinite ? d : nil
        } else if let s = try? c?.decode(String.self), let d = Double(s.trimmingCharacters(in: .whitespaces)) {
            value = d.isFinite ? d : nil
        } else {
            value = nil
        }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(value)
    }
}

/// `economics` on a reopened week (mobile_api._schedule_economics): only the
/// forecast's sales are read here.
struct ScheduleEconomicsLite: Codable, Equatable {
    var projectedRevenue: Double? = nil
    enum CodingKeys: String, CodingKey { case projectedRevenue = "projected_revenue" }
    init(projectedRevenue: Double? = nil) { self.projectedRevenue = projectedRevenue }
    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        projectedRevenue = (try? c?.decodeIfPresent(LenientDouble.self, forKey: .projectedRevenue))??.value
    }
}
