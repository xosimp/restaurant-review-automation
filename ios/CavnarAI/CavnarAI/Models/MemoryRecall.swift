import Foundation

// What Cavnar AI now remembers, as the owner's module screens show it (the
// memory round, 9/29/26 — the iOS twin of the web's Labor, Reviews,
// Marketing, Food Cost, Intel and What Connects additions). Every type
// decodes leniently: the payloads are additive, so a missing field is nil,
// an unknown kind is kept as text, and a malformed entry is skipped
// (`HomeLenientList`) — never a screen that fails to decode.

private extension KeyedDecodingContainer {
    /// Text, trimmed; a number the server sent is read as its text. Nil
    /// when absent or empty.
    func mrText(_ key: Key) -> String? {
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

    func mrInt(_ key: Key) -> Int? {
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i }
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return Int(d.rounded()) }
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil { return Int(s) }
        return nil
    }

    func mrDouble(_ key: Key) -> Double? {
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return d }
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil { return Double(s) }
        return nil
    }

    func mrBool(_ key: Key) -> Bool? {
        if let b = (try? decodeIfPresent(Bool.self, forKey: key)) ?? nil { return b }
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i != 0 }
        return nil
    }

    func mrList<T: Codable & Hashable>(_ type: T.Type, _ key: Key) -> [T] {
        ((try? decodeIfPresent(HomeLenientList<T>.self, forKey: key)) ?? nil)?.items ?? []
    }

    /// A list of words; a single string is a list of one.
    func mrWords(_ key: Key) -> [String] {
        if let a = (try? decodeIfPresent([String].self, forKey: key)) ?? nil { return a.filter { !$0.isEmpty } }
        if let s = mrText(key) { return [s] }
        return []
    }
}

extension CavnarDate {
    /// An ISO week ("2026-W38", own_rating_history's key) as the Monday it
    /// starts, M/D/YY. The text as it came when it isn't one.
    static func isoWeekStart(_ week: String) -> String {
        let parts = week.uppercased().split(separator: "W")
        guard parts.count == 2,
              let year = Int(parts[0].trimmingCharacters(in: CharacterSet(charactersIn: "-"))),
              let number = Int(parts[1]) else { return week }
        var cal = Calendar(identifier: .iso8601)
        cal.timeZone = TimeZone(identifier: "UTC") ?? .current
        var c = DateComponents()
        c.yearForWeekOfYear = year
        c.weekOfYear = number
        c.weekday = 2
        guard let date = cal.date(from: c) else { return week }
        return mdy(date, in: cal.timeZone)
    }
}

// MARK: - Labor: scheduling notes (GET /labor/staff-notes)

/// One constraint inside a person's scheduling note, dated: "noted 9/2/26 ·
/// ends 10/1/26". `stale` asks the owner "still true?" (over
/// `stale_after_days`); `ended` is kept on screen, greyed.
struct StaffNotePart: Codable, Hashable, Identifiable {
    let index: Int
    let text: String
    let noted: String?
    let notedOn: String?
    let expires: String?
    let expiresOn: String?
    let ended: Bool
    let stale: Bool
    var id: Int { index }

    enum CodingKeys: String, CodingKey {
        case index, text, noted, expires, ended, stale
        case notedOn = "noted_on"
        case expiresOn = "expires_on"
    }

    init(index: Int, text: String, noted: String? = nil, notedOn: String? = nil, expires: String? = nil,
         expiresOn: String? = nil, ended: Bool = false, stale: Bool = false) {
        self.index = index; self.text = text; self.noted = noted; self.notedOn = notedOn
        self.expires = expires; self.expiresOn = expiresOn; self.ended = ended; self.stale = stale
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let index = c.mrInt(.index), let text = c.mrText(.text) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no part"))
        }
        self.index = index
        self.text = text
        noted = c.mrText(.noted)
        notedOn = c.mrText(.notedOn)
        expires = c.mrText(.expires)
        expiresOn = c.mrText(.expiresOn)
        ended = c.mrBool(.ended) ?? false
        stale = c.mrBool(.stale) ?? false
    }

    /// "noted 9/2/26 · ends 10/1/26" — the server's M/D/YY labels, else its
    /// ISO dates put through the one formatter. Nil when neither is known.
    var dateLine: String? {
        var bits: [String] = []
        if let d = noted ?? notedOn.map(CavnarDate.mdy) { bits.append("noted " + d) }
        if let e = expires ?? expiresOn.map(CavnarDate.mdy) { bits.append((ended ? "ended " : "ends ") + e) }
        return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
    }
}

struct StaffNote: Codable, Hashable, Identifiable {
    let id: Int
    let employeeName: String
    let notes: String
    let noted: String?
    let stale: Bool
    let parts: [StaffNotePart]

    enum CodingKeys: String, CodingKey {
        case id, notes, noted, stale, parts
        case employeeName = "employee_name"
    }

    init(id: Int, employeeName: String, notes: String = "", noted: String? = nil, stale: Bool = false,
         parts: [StaffNotePart]) {
        self.id = id; self.employeeName = employeeName; self.notes = notes; self.noted = noted
        self.stale = stale; self.parts = parts
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.mrInt(.id) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no id"))
        }
        self.id = id
        employeeName = c.mrText(.employeeName) ?? ""
        notes = c.mrText(.notes) ?? ""
        noted = c.mrText(.noted)
        stale = c.mrBool(.stale) ?? false
        let parts = c.mrList(StaffNotePart.self, .parts)
        // A note from before parts were dated is one undated constraint.
        self.parts = parts.isEmpty && !notes.isEmpty ? [StaffNotePart(index: 0, text: notes)] : parts
    }

    var staleParts: [StaffNotePart] { parts.filter { $0.stale && !$0.ended } }
}

struct StaffNotesPayload: Decodable {
    let ok: Bool
    let notes: [StaffNote]
    let staleAfterDays: Int?
    let stale: Int
    let canEdit: Bool
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, notes, stale, error
        case staleAfterDays = "stale_after_days"
        case canEdit = "can_edit"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mrBool(.ok) ?? false
        notes = c.mrList(StaffNote.self, .notes)
        staleAfterDays = c.mrInt(.staleAfterDays)
        stale = c.mrInt(.stale) ?? notes.reduce(0) { $0 + $1.staleParts.count }
        canEdit = c.mrBool(.canEdit) ?? false
        error = c.mrText(.error)
    }
}

// MARK: - People: who is who (GET /people/identity)

/// Two records that may be one person — only the owner answers, and
/// nothing is merged on a guess.
struct IdentityQuestion: Codable, Hashable, Identifiable {
    struct Side: Codable, Hashable {
        let personId: Int?
        let name: String
        let key: String?
        let rated: Bool
        let sources: [String]

        enum CodingKeys: String, CodingKey {
            case name, key, rated, sources
            case personId = "person_id"
        }

        init(personId: Int? = nil, name: String, key: String? = nil, rated: Bool = false, sources: [String] = []) {
            self.personId = personId; self.name = name; self.key = key; self.rated = rated; self.sources = sources
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            personId = c.mrInt(.personId)
            name = c.mrText(.name) ?? "Someone"
            key = c.mrText(.key)
            rated = c.mrBool(.rated) ?? false
            sources = c.mrWords(.sources)
        }

        /// "rated · from the POS" — what each record rests on, so the owner
        /// can tell them apart. Nil when nothing is known.
        var detail: String? {
            var bits: [String] = []
            if rated { bits.append("rated") }
            let from = sources.map(Self.sourceLabel).filter { !$0.isEmpty }
            if !from.isEmpty { bits.append("from " + from.joined(separator: ", ")) }
            return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
        }

        static func sourceLabel(_ s: String) -> String {
            switch s.lowercased() {
            case "rpower": return "RPOWER"
            case "toast": return "Toast"
            case "square": return "Square"
            case "clover": return "Clover"
            case "upload", "csv": return "an upload"
            case "roster", "manual": return "the roster"
            case "portal", "login": return "a staff login"
            default: return s.replacingOccurrences(of: "_", with: " ")
            }
        }
    }

    let id: Int
    let kind: String?
    let reason: String?
    let a: Side
    let b: Side
    let asked: String?

    enum CodingKeys: String, CodingKey { case id, kind, reason, a, b, asked }

    init(id: Int, kind: String? = nil, reason: String? = nil, a: Side, b: Side, asked: String? = nil) {
        self.id = id; self.kind = kind; self.reason = reason; self.a = a; self.b = b; self.asked = asked
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.mrInt(.id), let a = try? c.decode(Side.self, forKey: .a),
              let b = try? c.decode(Side.self, forKey: .b) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no question"))
        }
        self.id = id; self.a = a; self.b = b
        kind = c.mrText(.kind)
        reason = c.mrText(.reason)
        asked = c.mrText(.asked)
    }

    /// "Is Kim T. the same person as Kim Tran?"
    var question: String { "Is \(a.name) the same person as \(b.name)?" }
}

struct IdentityPayload: Decodable {
    let ok: Bool
    let questions: [IdentityQuestion]
    let canAnswer: Bool

    enum CodingKeys: String, CodingKey {
        case ok, questions
        case canAnswer = "can_answer"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mrBool(.ok) ?? false
        questions = c.mrList(IdentityQuestion.self, .questions)
        canAnswer = c.mrBool(.canAnswer) ?? false
    }
}

// MARK: - People: guests naming the team (GET /people/mentions)

struct GuestMention: Codable, Hashable, Identifiable {
    let id: Int
    let name: String
    let key: String?
    let date: String?
    let dateIso: String?
    let polarity: String?
    let reviewId: String?
    let snippet: String?
    let status: String?

    enum CodingKeys: String, CodingKey {
        case id, name, key, date, polarity, snippet, status
        case dateIso = "date_iso"
        case reviewId = "review_id"
    }

    init(id: Int, name: String, key: String? = nil, date: String? = nil, dateIso: String? = nil,
         polarity: String? = nil, reviewId: String? = nil, snippet: String? = nil, status: String? = nil) {
        self.id = id; self.name = name; self.key = key; self.date = date; self.dateIso = dateIso
        self.polarity = polarity; self.reviewId = reviewId; self.snippet = snippet; self.status = status
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.mrInt(.id) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no id"))
        }
        self.id = id
        name = c.mrText(.name) ?? "Someone"
        key = c.mrText(.key)
        date = c.mrText(.date)
        dateIso = c.mrText(.dateIso)
        polarity = c.mrText(.polarity)
        reviewId = c.mrText(.reviewId)
        snippet = c.mrText(.snippet)
        status = c.mrText(.status)
    }

    /// The day in M/D/YY — the server's label, else its ISO date formatted.
    var dateLabel: String? { date ?? dateIso.map(CavnarDate.mdy) }

    var isPraise: Bool { polarity?.lowercased() == "positive" || polarity?.lowercased() == "praise" }
    var isComplaint: Bool { polarity?.lowercased() == "negative" || polarity?.lowercased() == "complaint" }
}

struct MentionsPayload: Decodable {
    let ok: Bool
    let mentions: [GuestMention]
    let canConfirm: Bool

    enum CodingKeys: String, CodingKey {
        case ok, mentions
        case canConfirm = "can_confirm"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.mrBool(.ok) ?? false
        mentions = c.mrList(GuestMention.self, .mentions)
        canConfirm = c.mrBool(.canConfirm) ?? false
    }
}

// MARK: - People: the person record's memory (GET /people/<key>)

/// A role held beyond the shifts worked — a promotion (`primary`) or
/// "trained on bar from 9/1/26".
struct PersonRole: Codable, Hashable, Identifiable {
    let role: String
    let since: String?
    let primary: Bool
    var id: String { role.lowercased() }

    enum CodingKeys: String, CodingKey { case role, since, primary }

    init(role: String, since: String? = nil, primary: Bool = false) {
        self.role = role; self.since = since; self.primary = primary
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let role = c.mrText(.role) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no role"))
        }
        self.role = role
        since = c.mrText(.since)
        primary = c.mrBool(.primary) ?? false
    }

    /// "Bartender · since 9/1/26 · their role on the roster"
    var line: String {
        var bits = [role.capitalized]
        if let since { bits.append("since " + CavnarDate.mdy(since)) }
        if primary { bits.append("their role on the roster") }
        return bits.joined(separator: " \u{00B7} ")
    }
}

/// Their record of taking covers, over `days`.
struct PersonCovers: Codable, Hashable {
    let taken: Int
    let declined: Int
    let days: Int?

    enum CodingKeys: String, CodingKey { case taken, declined, days }

    init(taken: Int, declined: Int, days: Int? = nil) { self.taken = taken; self.declined = declined; self.days = days }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        taken = c.mrInt(.taken) ?? 0
        declined = c.mrInt(.declined) ?? 0
        days = c.mrInt(.days)
    }

    /// "Took 3 of 4 covers asked · 180 days" — "No covers asked yet · 180
    /// days" when there is nothing on record, never a clean record.
    var line: String {
        let window = days.map { " \u{00B7} \($0) days" } ?? ""
        let asked = taken + declined
        if asked == 0 { return "No covers asked yet" + window }
        return "Took \(taken) of \(asked) cover\(asked == 1 ? "" : "s") asked" + window
    }
}

/// Attendance on the shifts somebody watched. `known: false` is "Not
/// watched yet" — unknown, never a clean record.
struct PersonAttendance: Codable, Hashable {
    let known: Bool
    let shifts: Int?
    let missed: Int?
    let late: Int?
    let noShowRate: Double?
    let unreliable: Bool
    let lastMiss: String?

    enum CodingKeys: String, CodingKey {
        case known, shifts, missed, late, unreliable
        case noShowRate = "no_show_rate"
        case lastMiss = "last_miss"
    }

    init(known: Bool, shifts: Int? = nil, missed: Int? = nil, late: Int? = nil, noShowRate: Double? = nil,
         unreliable: Bool = false, lastMiss: String? = nil) {
        self.known = known; self.shifts = shifts; self.missed = missed; self.late = late
        self.noShowRate = noShowRate; self.unreliable = unreliable; self.lastMiss = lastMiss
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        known = c.mrBool(.known) ?? false
        shifts = c.mrInt(.shifts)
        missed = c.mrInt(.missed)
        late = c.mrInt(.late)
        noShowRate = c.mrDouble(.noShowRate)
        unreliable = c.mrBool(.unreliable) ?? false
        lastMiss = c.mrText(.lastMiss)
    }

    /// "Missed 1 of 24 watched shifts · late 2 · last miss 9/3/26", or "Not
    /// watched yet".
    var line: String {
        guard known, let shifts else { return "Not watched yet" }
        var bits = ["Missed \(missed ?? 0) of \(shifts) watched shift\(shifts == 1 ? "" : "s")"]
        if let late, late > 0 { bits.append("late \(late)") }
        if let lastMiss { bits.append("last miss " + CavnarDate.mdy(lastMiss)) }
        return bits.joined(separator: " \u{00B7} ")
    }
}

// MARK: - Labor: standing patterns (GET /labor/learned-patterns)

/// A pattern the draft keeps after the manager stopped correcting it, with
/// who taught it, when it was learned and last kept, and whether it can
/// become the person's rule.
struct StandingPattern: Codable, Hashable, Identifiable {
    struct Rule: Codable, Hashable {
        let note: String?
        let by: String?
    }

    let key: String
    let kind: String?
    let employee: String?
    let day: String?
    let daypart: String?
    let text: String
    let editors: [String]
    let firstLearned: String?
    let lastConfirmed: String?
    let timesApplied: Int?
    let timesOverridden: Int?
    let status: String
    let canBeRule: Bool
    let rule: Rule?
    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, kind, employee, day, daypart, text, editors, status, rule
        case firstLearned = "first_learned"
        case lastConfirmed = "last_confirmed"
        case timesApplied = "times_applied"
        case timesOverridden = "times_overridden"
        case canBeRule = "can_be_rule"
    }

    init(key: String, kind: String? = nil, employee: String? = nil, day: String? = nil, daypart: String? = nil,
         text: String, editors: [String] = [], firstLearned: String? = nil, lastConfirmed: String? = nil,
         timesApplied: Int? = nil, timesOverridden: Int? = nil, status: String = "active",
         canBeRule: Bool = false, rule: Rule? = nil) {
        self.key = key; self.kind = kind; self.employee = employee; self.day = day; self.daypart = daypart
        self.text = text; self.editors = editors; self.firstLearned = firstLearned
        self.lastConfirmed = lastConfirmed; self.timesApplied = timesApplied
        self.timesOverridden = timesOverridden; self.status = status; self.canBeRule = canBeRule; self.rule = rule
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let key = c.mrText(.key) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no key"))
        }
        self.key = key
        kind = c.mrText(.kind)
        employee = c.mrText(.employee)
        day = c.mrText(.day)
        daypart = c.mrText(.daypart)
        text = c.mrText(.text) ?? key
        // {editor: times} on the server; a plain list reads too.
        if let map = (try? c.decodeIfPresent([String: JSONValue].self, forKey: .editors)) ?? nil {
            editors = map.keys.sorted()
        } else {
            editors = c.mrWords(.editors)
        }
        firstLearned = c.mrText(.firstLearned)
        lastConfirmed = c.mrText(.lastConfirmed)
        timesApplied = c.mrInt(.timesApplied)
        timesOverridden = c.mrInt(.timesOverridden)
        status = c.mrText(.status)?.lowercased() ?? "active"
        canBeRule = c.mrBool(.canBeRule) ?? false
        rule = try? c.decodeIfPresent(Rule.self, forKey: .rule)
    }

    /// "learned 9/7/26 · last kept 9/21/26 · taught by Dana" — dates as the
    /// server wrote them (M/D/YY).
    var historyLine: String? {
        var bits: [String] = []
        if let firstLearned { bits.append("learned " + firstLearned) }
        if let lastConfirmed, lastConfirmed != firstLearned { bits.append("last kept " + lastConfirmed) }
        if !editors.isEmpty { bits.append("taught by " + editors.joined(separator: ", ")) }
        return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
    }

    /// The status in words — an unknown status is shown as the server
    /// wrote it.
    var statusLabel: String {
        switch status {
        case "active": return "in use"
        case "ruled": return "now a rule"
        case "retired": return "retired \u{2014} reversed twice"
        case "dismissed": return "not in use"
        default: return status.replacingOccurrences(of: "_", with: " ")
        }
    }
}

/// A pair two editors pull opposite ways — the owner settles it, never the
/// model.
struct PatternConflict: Codable, Hashable, Identifiable {
    let employee: String
    let day: String?
    let daypart: String?
    let offBy: [String]
    let onBy: [String]
    var id: String { [employee, day ?? "", daypart ?? ""].joined(separator: "|") }

    enum CodingKeys: String, CodingKey {
        case employee, day, daypart
        case offBy = "off_by"
        case onBy = "on_by"
    }

    init(employee: String, day: String? = nil, daypart: String? = nil, offBy: [String] = [], onBy: [String] = []) {
        self.employee = employee; self.day = day; self.daypart = daypart; self.offBy = offBy; self.onBy = onBy
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        employee = c.mrText(.employee) ?? "Someone"
        day = c.mrText(.day)
        daypart = c.mrText(.daypart)
        offBy = c.mrWords(.offBy)
        onBy = c.mrWords(.onBy)
    }

    /// "Dana, Friday night: Sam takes them off, Lee puts them on — settle it
    /// in the roster."
    var line: String {
        let when = [day, daypart].compactMap { $0 }.joined(separator: " ")
        let off = offBy.isEmpty ? "one editor" : offBy.joined(separator: ", ")
        let on = onBy.isEmpty ? "another" : onBy.joined(separator: ", ")
        return "\(employee)\(when.isEmpty ? "" : ", " + when): \(off) takes them off, \(on) puts them on"
    }
}

// MARK: - Labor: the drafted week (schedule result)

/// A soft staffing requirement the draft read — from the reviews
/// diagnosis or a nightly report — and whether the rows honoured it.
struct SoftRequirement: Codable, Hashable, Identifiable {
    let source: String?
    let day: String?
    let date: String?
    let daypart: String?
    let role: String?
    let text: String
    let confirm: String?
    let applied: Bool?
    let scheduled: Int?
    let expires: String?
    var id: String { [date ?? "", daypart ?? "", role ?? "", text].joined(separator: "|") }

    enum CodingKeys: String, CodingKey {
        case source, day, date, daypart, role, text, confirm, applied, scheduled, expires
    }

    init(source: String? = nil, day: String? = nil, date: String? = nil, daypart: String? = nil,
         role: String? = nil, text: String, confirm: String? = nil, applied: Bool? = nil,
         scheduled: Int? = nil, expires: String? = nil) {
        self.source = source; self.day = day; self.date = date; self.daypart = daypart; self.role = role
        self.text = text; self.confirm = confirm; self.applied = applied; self.scheduled = scheduled
        self.expires = expires
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let text = c.mrText(.text) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no text"))
        }
        self.text = text
        source = c.mrText(.source)
        day = c.mrText(.day)
        date = c.mrText(.date)
        daypart = c.mrText(.daypart)
        role = c.mrText(.role)
        confirm = c.mrText(.confirm)
        applied = c.mrBool(.applied)
        scheduled = c.mrInt(.scheduled)
        expires = c.mrText(.expires)
    }

    /// Where it came from, in the owner's words.
    var sourceLabel: String {
        switch (source ?? "").lowercased() {
        case "reviews": return "Reviews"
        case "dsr": return "Nightly report"
        default: return (source ?? "").isEmpty ? "Cavnar AI" : source!.capitalized
        }
    }

    /// "Applied" / "Not applied" once the rows were read; nil before.
    var appliedLabel: String? {
        guard let applied else { return nil }
        return applied ? "Applied" : "Not applied"
    }
}

/// Why a kind of schedule recommendation is quiet, and when it is
/// re-tested (`recommendation_suppression`, keyed by kind).
struct SuppressionState: Codable, Hashable, Identifiable {
    let kind: String
    let state: String?
    let reason: String?
    let since: String?
    let reviewOn: String?
    let retests: Int?
    var id: String { kind }

    enum CodingKeys: String, CodingKey {
        case state, reason, since, retests
        case reviewOn = "review_on"
    }

    init(kind: String, state: String? = nil, reason: String? = nil, since: String? = nil,
         reviewOn: String? = nil, retests: Int? = nil) {
        self.kind = kind; self.state = state; self.reason = reason; self.since = since
        self.reviewOn = reviewOn; self.retests = retests
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.codingPath.last?.stringValue ?? ""
        state = c.mrText(.state)
        reason = c.mrText(.reason)
        since = c.mrText(.since)
        reviewOn = c.mrText(.reviewOn)
        retests = c.mrInt(.retests)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(state, forKey: .state)
        try c.encodeIfPresent(reason, forKey: .reason)
        try c.encodeIfPresent(since, forKey: .since)
        try c.encodeIfPresent(reviewOn, forKey: .reviewOn)
        try c.encodeIfPresent(retests, forKey: .retests)
    }

    /// "Trim day — quiet since 9/1/26: the last four went unanswered.
    /// Re-tested on 10/1/26." The server's M/D/YY dates; the kind in words.
    var line: String {
        let name = kind.replacingOccurrences(of: "_", with: " ").capitalized
        var s = name
        if state == "retest" { s += " \u{2014} being re-tested" }
        else if let since { s += " \u{2014} quiet since \(Self.date(since))" }
        else { s += " \u{2014} quiet" }
        if let reason { s += ": " + reason.trimmingCharacters(in: CharacterSet(charactersIn: ".")) }
        if let reviewOn { s += ". Re-tested on \(Self.date(reviewOn))" }
        return s + "."
    }

    private static func date(_ s: String) -> String { s.contains("-") ? CavnarDate.mdy(s) : s }
}

/// The map `{kind: {state, reason, since, review_on, retests}}` as a list.
struct SuppressionMap: Codable, Hashable {
    let items: [SuppressionState]

    init(_ items: [SuppressionState] = []) { self.items = items }

    init(from decoder: Decoder) throws {
        guard let map = try? decoder.singleValueContainer().decode([String: SuppressionState].self) else {
            items = []
            return
        }
        items = map.map { k, v in
            SuppressionState(kind: k, state: v.state, reason: v.reason, since: v.since, reviewOn: v.reviewOn,
                             retests: v.retests)
        }.sorted { $0.kind < $1.kind }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(Dictionary(uniqueKeysWithValues: items.map { ($0.kind, $0) }))
    }
}

// MARK: - Labor: rating and target history (GET /labor/capability-changes)

/// Who changed a rating, a target, a profile or the weighting, and when.
struct CapabilityChange: Decodable, Hashable, Identifiable {
    let id: Int
    let kind: String
    let subject: String?
    let before: JSONValue?
    let after: JSONValue?
    let changedBy: String?
    let changedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, subject, before, after
        case changedBy = "changed_by"
        case changedAt = "changed_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.mrInt(.id) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no id"))
        }
        self.id = id
        kind = c.mrText(.kind) ?? "change"
        subject = c.mrText(.subject)
        before = try? c.decodeIfPresent(JSONValue.self, forKey: .before)
        after = try? c.decodeIfPresent(JSONValue.self, forKey: .after)
        changedBy = c.mrText(.changedBy)
        changedAt = c.mrText(.changedAt)
    }

    var kindLabel: String {
        switch kind {
        case "rating": return "Rating"
        case "threshold": return "Target"
        case "leader_rule": return "Leader rule"
        case "profile": return "Shift profile"
        case "weights": return "Weighting"
        default: return kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    /// "Rating · Dana: 3 → 4, by Will on 9/12/26".
    var line: String {
        var s = kindLabel
        if let subject { s += " \u{00B7} " + subject }
        let from = Self.value(before), to = Self.value(after)
        switch (from, to) {
        case let (f?, t?): s += ": \(f) \u{2192} \(t)"
        case let (nil, t?): s += ": set to \(t)"
        case (_?, nil): s += ": cleared"
        default: break
        }
        if let changedBy { s += ", by " + changedBy }
        if let changedAt { s += " on " + CavnarDate.mdy(changedAt) }
        return s
    }

    /// One readable value from whatever shape the change stored: a score,
    /// a target, or the object's own fields.
    static func value(_ v: JSONValue?) -> String? {
        guard let v else { return nil }
        switch v {
        case .null: return nil
        case .string(let s): return s.isEmpty ? nil : s
        case .bool(let b): return b ? "on" : "off"
        case .number(let n): return n == n.rounded() ? String(Int(n)) : String(format: "%.1f", n)
        case .array(let a):
            let parts = a.compactMap { value($0) }
            return parts.isEmpty ? nil : parts.joined(separator: ", ")
        case .object(let o):
            for k in ["score", "target", "value", "min", "label"] {
                if let x = o[k].flatMap({ value($0) }) { return x }
            }
            let parts = o.keys.sorted().compactMap { k -> String? in
                value(o[k]).map { "\(k.replacingOccurrences(of: "_", with: " ")) \($0)" }
            }
            return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
        }
    }
}

// MARK: - Events & reservations (GET /labor/demand-signals)

/// A listed event's measured record here: "nights like this ran a median
/// 14% above a typical Friday (measured 4 times)".
struct EventMeasured: Codable, Hashable {
    let medianLiftPct: Double?
    let n: Int
    let applies: Bool
    let last: String?
    let text: String?

    enum CodingKeys: String, CodingKey {
        case n, applies, last, text
        case medianLiftPct = "median_lift_pct"
    }

    init(medianLiftPct: Double?, n: Int, applies: Bool, last: String? = nil, text: String? = nil) {
        self.medianLiftPct = medianLiftPct; self.n = n; self.applies = applies; self.last = last; self.text = text
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        medianLiftPct = c.mrDouble(.medianLiftPct)
        n = c.mrInt(.n) ?? 0
        applies = c.mrBool(.applies) ?? false
        last = c.mrText(.last)
        text = c.mrText(.text)
    }

    /// The server's sentence; else one built from the figures.
    var line: String {
        if let text { return text }
        guard let pct = medianLiftPct else { return "Measured \(n) time\(n == 1 ? "" : "s") here" }
        let word = pct >= 0 ? "above" : "below"
        return "Nights like this ran a median \(Int(abs(pct).rounded()))% \(word) a typical same weekday "
            + "(measured \(n) time\(n == 1 ? "" : "s")) \u{2014} before and after, not proof"
    }
}

/// One recurring effect the nights taught (`what_nights_teach`).
struct NightLesson: Codable, Hashable, Identifiable {
    let label: String
    let display: String?
    let kind: String?
    let n: Int
    let medianLiftPct: Double?
    let applies: Bool
    let text: String
    var id: String { label }

    enum CodingKeys: String, CodingKey {
        case label, display, kind, n, applies, text
        case medianLiftPct = "median_lift_pct"
    }

    init(label: String, display: String? = nil, kind: String? = nil, n: Int, medianLiftPct: Double? = nil,
         applies: Bool, text: String) {
        self.label = label; self.display = display; self.kind = kind; self.n = n
        self.medianLiftPct = medianLiftPct; self.applies = applies; self.text = text
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let text = c.mrText(.text) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no text"))
        }
        self.text = text
        label = c.mrText(.label) ?? text
        display = c.mrText(.display)
        kind = c.mrText(.kind)
        n = c.mrInt(.n) ?? 0
        medianLiftPct = c.mrDouble(.medianLiftPct)
        applies = c.mrBool(.applies) ?? false
    }
}

// MARK: - Reviews

/// How many guests asked for a review left one (`review-request-stats`
/// `conversion`). `pct` is nil below its floor — shown as "—".
struct RequestConversion: Codable, Hashable {
    let asked: Int
    let reviewed: Int
    let pct: Double?
    let windowDays: Int?
    let basis: String?

    enum CodingKeys: String, CodingKey {
        case asked, reviewed, pct, basis
        case windowDays = "window_days"
    }

    init(asked: Int, reviewed: Int, pct: Double?, windowDays: Int? = nil, basis: String? = nil) {
        self.asked = asked; self.reviewed = reviewed; self.pct = pct; self.windowDays = windowDays; self.basis = basis
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        asked = c.mrInt(.asked) ?? 0
        reviewed = c.mrInt(.reviewed) ?? 0
        pct = c.mrDouble(.pct)
        windowDays = c.mrInt(.windowDays)
        basis = c.mrText(.basis)
    }

    /// "3 of 10 guests you asked left a review within 14 days (30%)", the
    /// percentage "—" below the floor.
    var line: String {
        let within = windowDays.map { " within \($0) days" } ?? ""
        let pctText = pct.map { "\(Int($0.rounded()))%" } ?? "\u{2014}"
        return "\(reviewed) of \(asked) guest\(asked == 1 ? "" : "s") you asked left a review\(within) (\(pctText))"
    }
}

/// The analyser's own vocabulary for a re-tag — the same keys and words
/// as analyser.CATEGORY_LABELS / SEVERITY_LABELS (pinned by
/// tests/test_mem_ui_ib.py, so a new category reaches the phone).
enum ReviewTagVocabulary {
    static let categories: [(key: String, label: String)] = [
        ("food_quality", "food quality"),
        ("service", "service"),
        ("wait_time", "wait time"),
        ("value", "value for money"),
        ("ambiance", "atmosphere"),
        ("cleanliness", "cleanliness"),
        ("reservation", "reservations"),
        ("takeout_delivery", "takeout and delivery"),
    ]
    static let severities: [(key: String, label: String)] = [
        ("safety", "Guest safety"),
        ("legal", "Legal exposure"),
        ("operational", "Operational failure"),
        ("service", "Service quality"),
        ("minor", "Minor"),
    ]
    static let sentiments: [String] = ["positive", "neutral", "negative"]

    static func categoryLabel(_ key: String) -> String {
        categories.first { $0.key == key }?.label ?? key.replacingOccurrences(of: "_", with: " ")
    }
}

// MARK: - Marketing

/// What past texts did per audience (`returns_by_segment`, keyed by
/// segment): "Haven't been in 60+ days: 9 came back per 100 texted (2
/// campaigns)".
struct SegmentReturn: Codable, Hashable, Identifiable {
    let segment: String
    let label: String
    let backPer100: Double?
    let campaigns: Int
    let sent: Int?
    let cameBack: Int?
    var id: String { segment }

    enum CodingKeys: String, CodingKey {
        case label, campaigns, sent
        case backPer100 = "back_per_100"
        case cameBack = "came_back"
    }

    init(segment: String, label: String, backPer100: Double?, campaigns: Int, sent: Int? = nil, cameBack: Int? = nil) {
        self.segment = segment; self.label = label; self.backPer100 = backPer100; self.campaigns = campaigns
        self.sent = sent; self.cameBack = cameBack
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        segment = c.codingPath.last?.stringValue ?? ""
        label = c.mrText(.label) ?? segment.replacingOccurrences(of: "_", with: " ").capitalized
        backPer100 = c.mrDouble(.backPer100)
        campaigns = c.mrInt(.campaigns) ?? 0
        sent = c.mrInt(.sent)
        cameBack = c.mrInt(.cameBack)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(label, forKey: .label)
        try c.encodeIfPresent(backPer100, forKey: .backPer100)
        try c.encode(campaigns, forKey: .campaigns)
        try c.encodeIfPresent(sent, forKey: .sent)
        try c.encodeIfPresent(cameBack, forKey: .cameBack)
    }

    var line: String {
        let figure = backPer100.map { $0 == $0.rounded() ? String(Int($0)) : String(format: "%.1f", $0) } ?? "\u{2014}"
        return "\(label): \(figure) came back per 100 texted (\(campaigns) campaign\(campaigns == 1 ? "" : "s"))"
    }

    /// `{segment: {...}}` as a list, best first.
    static func list(from map: [String: SegmentReturn]?) -> [SegmentReturn] {
        (map ?? [:]).map { k, v in
            SegmentReturn(segment: k, label: v.label, backPer100: v.backPer100, campaigns: v.campaigns,
                          sent: v.sent, cameBack: v.cameBack)
        }.sorted { ($0.backPer100 ?? -1) > ($1.backPer100 ?? -1) }
    }
}

// MARK: - Food Cost

/// An order line the owner's own habit adjusted: "your last 5 orders sent
/// about 80% of what the draft suggested for this item".
struct OrderAdjustment: Codable, Hashable {
    let factor: Double?
    let orders: Int?
    let basis: String?

    init(factor: Double?, orders: Int?, basis: String?) { self.factor = factor; self.orders = orders; self.basis = basis }

    enum CodingKeys: String, CodingKey { case factor, orders, basis }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        factor = c.mrDouble(.factor)
        orders = c.mrInt(.orders)
        basis = c.mrText(.basis)
    }

    /// "Adjusted to how you order: …" — the basis, else the factor.
    func line(baseQty: Int?) -> String {
        if let basis { return "Adjusted to how you order: " + basis }
        var s = "Adjusted to how you order"
        if let factor { s += ": \(Int((factor * 100).rounded()))% of the formula" }
        if let baseQty { s += " (the formula said \(baseQty))" }
        return s
    }
}

/// This owner's usual choice on a reprice (`owner_ratio`).
struct RepriceOwnerRatio: Codable, Hashable {
    let ratio: Double?
    let decisions: Int?
    let basis: String?

    enum CodingKeys: String, CodingKey { case ratio, decisions, basis }

    init(ratio: Double?, decisions: Int? = nil, basis: String? = nil) {
        self.ratio = ratio; self.decisions = decisions; self.basis = basis
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ratio = c.mrDouble(.ratio)
        decisions = c.mrInt(.decisions)
        basis = c.mrText(.basis)
    }

    /// "about half", "about three quarters" — the owner's habit in words.
    var share: String? {
        guard let ratio else { return nil }
        switch ratio {
        case ..<0.35: return "about a third"
        case ..<0.6: return "about half"
        case ..<0.85: return "about three quarters"
        case ..<1.1: return "about all"
        default: return "more than"
        }
    }
}

/// A reprice held back by a live cross-module link: guests are naming the
/// dish in complaints — fix the plate before the price.
struct RepriceGuard: Codable, Hashable {
    let kind: String?
    let text: String
    let linkKey: String?
    let since: String?

    enum CodingKeys: String, CodingKey {
        case kind, text, since
        case linkKey = "link_key"
    }

    init(kind: String? = nil, text: String, linkKey: String? = nil, since: String? = nil) {
        self.kind = kind; self.text = text; self.linkKey = linkKey; self.since = since
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let text = c.mrText(.text) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no text"))
        }
        self.text = text
        kind = c.mrText(.kind)
        linkKey = c.mrText(.linkKey)
        since = c.mrText(.since)
    }
}

/// A par the close-out's 86s say is too low (GET /food-cost/par-suggestions).
struct ParSuggestion: Codable, Hashable, Identifiable {
    let ingredientId: Int
    let name: String
    let par: Double?
    let suggestedPar: Double?
    let times: Int?
    let last: String?
    let key: String?
    let basis: String?
    var id: Int { ingredientId }

    enum CodingKeys: String, CodingKey {
        case name, par, times, last, key, basis
        case ingredientId = "ingredient_id"
        case suggestedPar = "suggested_par"
    }

    init(ingredientId: Int, name: String, par: Double?, suggestedPar: Double?, times: Int? = nil,
         last: String? = nil, key: String? = nil, basis: String? = nil) {
        self.ingredientId = ingredientId; self.name = name; self.par = par; self.suggestedPar = suggestedPar
        self.times = times; self.last = last; self.key = key; self.basis = basis
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.mrInt(.ingredientId) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no item"))
        }
        ingredientId = id
        name = c.mrText(.name) ?? "An item"
        par = c.mrDouble(.par)
        suggestedPar = c.mrDouble(.suggestedPar)
        times = c.mrInt(.times)
        last = c.mrText(.last)
        key = c.mrText(.key)
        basis = c.mrText(.basis)
    }

    static func qty(_ v: Double?) -> String {
        guard let v else { return "\u{2014}" }
        return v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v)
    }

    /// "Raise the par on Salmon from 6 to 9"
    var title: String { "Raise the par on \(name) from \(Self.qty(par)) to \(Self.qty(suggestedPar))" }

    /// "86'd 3 times in 4 weeks, last 9/26/26" — the server's basis first.
    var why: String? {
        if let basis { return basis }
        guard let times else { return nil }
        var s = "Ran out \(times) time\(times == 1 ? "" : "s") in 4 weeks"
        if let last { s += ", last " + CavnarDate.mdy(last) }
        return s
    }
}

/// The month-end projection corrected by its own record: "already
/// corrected: earlier projections ran 12% high".
struct ProjectionCorrection: Codable, Hashable {
    let factor: Double?
    let biasPct: Double?
    let note: String?
    let primeCostPct: Double?
    let projectedPrimeCost: Double?

    enum CodingKeys: String, CodingKey {
        case factor, note
        case biasPct = "bias_pct"
        case primeCostPct = "prime_cost_pct"
        case projectedPrimeCost = "projected_prime_cost"
    }

    init(factor: Double?, biasPct: Double?, note: String?, primeCostPct: Double? = nil,
         projectedPrimeCost: Double? = nil) {
        self.factor = factor; self.biasPct = biasPct; self.note = note
        self.primeCostPct = primeCostPct; self.projectedPrimeCost = projectedPrimeCost
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        factor = c.mrDouble(.factor)
        biasPct = c.mrDouble(.biasPct)
        note = c.mrText(.note)
        primeCostPct = c.mrDouble(.primeCostPct)
        projectedPrimeCost = c.mrDouble(.projectedPrimeCost)
    }

    /// The server's note, else one from the lean.
    var line: String? {
        if let note { return note }
        guard let biasPct else { return nil }
        return "already corrected: earlier projections ran \(Int(abs(biasPct).rounded()))% "
            + (biasPct >= 0 ? "high" : "low")
    }
}

// MARK: - Intel: the market's history (GET /intel/movement)

struct MarketEvent: Codable, Hashable, Identifiable {
    let placeId: String?
    let name: String
    let kind: String
    let fromRating: Double?
    let toRating: Double?
    let observedOn: String?
    var id: String { [placeId ?? name, kind, observedOn ?? ""].joined(separator: "|") }

    enum CodingKeys: String, CodingKey {
        case name, kind
        case placeId = "place_id"
        case fromRating = "from_rating"
        case toRating = "to_rating"
        case observedOn = "observed_on"
    }

    init(placeId: String? = nil, name: String, kind: String, fromRating: Double? = nil, toRating: Double? = nil,
         observedOn: String? = nil) {
        self.placeId = placeId; self.name = name; self.kind = kind; self.fromRating = fromRating
        self.toRating = toRating; self.observedOn = observedOn
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        placeId = c.mrText(.placeId)
        name = c.mrText(.name) ?? "A competitor"
        kind = c.mrText(.kind)?.lowercased() ?? "moved"
        fromRating = c.mrDouble(.fromRating)
        toRating = c.mrDouble(.toRating)
        observedOn = c.mrText(.observedOn)
    }

    private static func stars(_ v: Double) -> String { String(format: "%.1f\u{2605}", v) }

    /// "Arrived nearby", "No longer nearby", "4.2★ → 4.5★" — an unknown
    /// kind reads as the server wrote it.
    var what: String {
        switch kind {
        case "arrived": return "Arrived nearby" + (toRating.map { " at " + Self.stars($0) } ?? "")
        case "gone": return "No longer nearby"
        case "rating_up", "rating_down":
            if let f = fromRating, let t = toRating { return Self.stars(f) + " \u{2192} " + Self.stars(t) }
            return kind == "rating_up" ? "Rating up" : "Rating down"
        default: return kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    var isRise: Bool { kind == "rating_up" }
    var isFall: Bool { kind == "rating_down" }
    var dateLabel: String? { observedOn.map(CavnarDate.mdy) }
}

/// The restaurant's own public rating over time, week by week.
struct OwnRatingHistory: Codable, Hashable {
    struct Point: Codable, Hashable {
        let week: String
        let rating: Double
        let reviewCount: Int?

        enum CodingKeys: String, CodingKey {
            case week, rating
            case reviewCount = "review_count"
        }

        init(week: String, rating: Double, reviewCount: Int? = nil) {
            self.week = week; self.rating = rating; self.reviewCount = reviewCount
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let week = c.mrText(.week), let rating = c.mrDouble(.rating) else {
                throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "no point"))
            }
            self.week = week; self.rating = rating
            reviewCount = c.mrInt(.reviewCount)
        }

        var weekLabel: String { CavnarDate.isoWeekStart(week) }
    }

    let available: Bool
    let first: Point?
    let latest: Point?
    let change: Double?
    let weeks: Int?
    let series: [Point]
    let reason: String?

    enum CodingKeys: String, CodingKey { case available, first, latest, change, weeks, series, reason }

    init(available: Bool, first: Point? = nil, latest: Point? = nil, change: Double? = nil, weeks: Int? = nil,
         series: [Point] = [], reason: String? = nil) {
        self.available = available; self.first = first; self.latest = latest; self.change = change
        self.weeks = weeks; self.series = series; self.reason = reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        series = c.mrList(Point.self, .series)
        first = (try? c.decodeIfPresent(Point.self, forKey: .first)) ?? series.first
        latest = (try? c.decodeIfPresent(Point.self, forKey: .latest)) ?? series.last
        available = (c.mrBool(.available) ?? false) && first != nil && latest != nil
        change = c.mrDouble(.change)
        weeks = c.mrInt(.weeks)
        reason = c.mrText(.reason)
    }

    /// "4.3★ the week of 3/2/26 → 4.6★ now (+0.3)"
    var line: String? {
        guard available, let first, let latest else { return nil }
        let delta = change ?? (latest.rating - first.rating)
        let sign = delta > 0 ? "+" : (delta < 0 ? "\u{2212}" : "\u{00B1}")
        return String(format: "%.1f\u{2605}", first.rating) + " the week of " + first.weekLabel
            + " \u{2192} " + String(format: "%.1f\u{2605}", latest.rating) + " now ("
            + sign + String(format: "%.1f", abs(delta)) + ")"
    }
}

// MARK: - What Connects: a link's memory (GET /cross-module)

/// How long a cross-module link has stood: "Found 3 weeks running, since
/// 9/7/26", "Still found after you marked it done on 9/14/26". `label` is
/// the server's sentence (M/D/YY).
struct LinkMemory: Codable, Hashable {
    let firstSeen: String?
    let lastSeen: String?
    let timesSeen: Int?
    let weeksRunning: Int?
    let recurring: Bool
    let cameBack: Bool
    let declined: Bool
    let resolved: Bool
    let label: String?

    enum CodingKeys: String, CodingKey {
        case recurring, declined, resolved, label
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
        case timesSeen = "times_seen"
        case weeksRunning = "weeks_running"
        case cameBack = "came_back"
    }

    init(firstSeen: String? = nil, lastSeen: String? = nil, timesSeen: Int? = nil, weeksRunning: Int? = nil,
         recurring: Bool = false, cameBack: Bool = false, declined: Bool = false, resolved: Bool = false,
         label: String? = nil) {
        self.firstSeen = firstSeen; self.lastSeen = lastSeen; self.timesSeen = timesSeen
        self.weeksRunning = weeksRunning; self.recurring = recurring; self.cameBack = cameBack
        self.declined = declined; self.resolved = resolved; self.label = label
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        firstSeen = c.mrText(.firstSeen)
        lastSeen = c.mrText(.lastSeen)
        timesSeen = c.mrInt(.timesSeen)
        weeksRunning = c.mrInt(.weeksRunning)
        recurring = c.mrBool(.recurring) ?? false
        // `came_back` is an object ({on, after, resolved_on}) or null.
        if let b = c.mrBool(.cameBack) {
            cameBack = b
        } else if let o = (try? c.decodeIfPresent([String: JSONValue].self, forKey: .cameBack)) ?? nil {
            cameBack = !o.isEmpty
        } else {
            cameBack = false
        }
        declined = c.mrBool(.declined) ?? false
        resolved = c.mrBool(.resolved) ?? false
        label = c.mrText(.label)
    }

    /// The sentence to show under a link's headline.
    var line: String? {
        if let label { return label }
        if let weeksRunning, weeksRunning >= 2, let firstSeen {
            return "Found \(weeksRunning) weeks running, since \(CavnarDate.mdy(firstSeen))"
        }
        return firstSeen.map { "First found " + CavnarDate.mdy($0) }
    }

    /// The badge beside the headline.
    var badge: String? {
        guard recurring else { return nil }
        return cameBack ? "Came back" : "Recurring"
    }
}
