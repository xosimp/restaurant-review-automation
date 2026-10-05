import Foundation

// The schedule fix round's setup payloads (schedule audit 10/3/26): who runs
// the floor, who stands in as the manager, the days somebody always works,
// who is in training, the closers per role, which job codes are one role,
// floors suggested from history, the labor standards, the salaried list and
// next week's forecast. Every type decodes leniently — an odd value is
// empty, never a roster or a rules sheet that fails to load — because each
// rides inside a payload the screen already depends on.

// MARK: - Lenient reading

extension KeyedDecodingContainer {
    /// Text, trimmed; a number is read as its text. Nil when absent or empty.
    func setupText(_ key: Key) -> String? {
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil {
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? nil : t
        }
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return String(i) }
        return nil
    }

    func setupInt(_ key: Key) -> Int? {
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i }
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return Int(d.rounded()) }
        return nil
    }

    func setupDouble(_ key: Key) -> Double? {
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil, d.isFinite { return d }
        if let s = (try? decodeIfPresent(String.self, forKey: key)) ?? nil { return Double(s) }
        return nil
    }

    /// A Bool; 1/0 read as true/false. Nil when absent, null or anything else.
    func setupBool(_ key: Key) -> Bool? {
        if let b = (try? decodeIfPresent(Bool.self, forKey: key)) ?? nil { return b }
        if let i = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return i != 0 }
        return nil
    }

    /// A list of words; anything that is not text is skipped.
    func setupTexts(_ key: Key) -> [String] {
        ((try? decodeIfPresent(HomeLenientListDecodable<String>.self, forKey: key)) ?? nil)?.items
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty } ?? []
    }

    /// A list read element by element: an entry that doesn't decode is skipped.
    func setupList<T: Decodable>(_ type: T.Type, _ key: Key) -> [T] {
        ((try? decodeIfPresent(HomeLenientListDecodable<T>.self, forKey: key)) ?? nil)?.items ?? []
    }
}

// MARK: - One person's scheduling facts (staff_settings, F1)

/// Whether somebody counts as the manager on the floor, and why — the
/// owner's yes or no (`set`), else automatic from their role, a role they
/// hold, a manager role worked lately or the floor manager certificate
/// (schedule_rules.manager_basis). GET labor/roster `floor_manager`.
struct FloorManagerStatus: Codable, Equatable {
    var counts: Bool = false
    var basis: String? = nil
    var why: String? = nil
    /// The owner's word: true, false, or nil for automatic.
    var set: Bool? = nil

    init(counts: Bool = false, basis: String? = nil, why: String? = nil, set: Bool? = nil) {
        self.counts = counts; self.basis = basis; self.why = why; self.set = set
    }

    enum CodingKeys: String, CodingKey { case counts, basis, why, set }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        counts = c.setupBool(.counts) ?? false
        basis = c.setupText(.basis)
        why = c.setupText(.why)
        set = c.setupBool(.set)
    }
}

/// Dates somebody stands in as the manager on duty (E-13), ISO dates.
struct ActingRange: Codable, Equatable, Hashable {
    var from: String
    var until: String
    var note: String? = nil

    init(from: String, until: String, note: String? = nil) {
        self.from = from; self.until = until; self.note = note
    }

    enum CodingKeys: String, CodingKey { case from, until, note }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        from = c.setupText(.from) ?? ""
        until = c.setupText(.until) ?? from
        note = c.setupText(.note)
    }

    /// "10/5/26 – 10/11/26" (one date when it is one day).
    var label: String { CavnarDate.mdyRange(from, until) }
}

/// A shift somebody always works (D-5): a weekday, times in the house form
/// ("10:00am"), an optional role and the dates it holds between.
struct StandingShift: Codable, Equatable, Hashable, Identifiable {
    var day: String
    var start: String
    var end: String
    var role: String? = nil
    var from: String? = nil
    var until: String? = nil
    /// Only on the days this event calendar plays ("nfl-chicago-bears"):
    /// Erik's Sunday is a Bears Sunday only (10/5/26). Kept on every save,
    /// or an edit here would turn it into every Sunday.
    var whenEvent: String? = nil

    init(day: String, start: String, end: String, role: String? = nil, from: String? = nil, until: String? = nil,
         whenEvent: String? = nil) {
        self.day = day; self.start = start; self.end = end; self.role = role; self.from = from; self.until = until
        self.whenEvent = whenEvent
    }

    var id: String { "\(day)|\(start)|\(end)|\(role ?? "")" }

    enum CodingKeys: String, CodingKey { case day, start, end, role, from, until, whenEvent = "when_event" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = c.setupText(.day) ?? ""
        start = c.setupText(.start) ?? ""
        end = c.setupText(.end) ?? ""
        role = c.setupText(.role)
        from = c.setupText(.from)
        until = c.setupText(.until)
        whenEvent = c.setupText(.whenEvent)
    }

    /// "the Bears" from "nfl-chicago-bears": the team the shift waits on.
    var eventName: String? {
        guard let slug = whenEvent, let last = slug.split(separator: "-").last, !last.isEmpty else { return nil }
        return "the " + last.prefix(1).uppercased() + last.dropFirst()
    }

    /// "Monday 10:00am–6:00pm · Manager FOH · until 12/15/26".
    var line: String {
        var s = "\(day) \(start)\u{2013}\(end)"
        if let role, !role.isEmpty { s += " \u{00B7} \(role)" }
        if let from, let until { s += " \u{00B7} " + CavnarDate.mdyRange(from, until) }
        else if let from { s += " \u{00B7} from " + CavnarDate.mdy(from) }
        else if let until { s += " \u{00B7} until " + CavnarDate.mdy(until) }
        if let eventName { s += " \u{00B7} only when \(eventName) play" }
        return s
    }
}

/// In training (D-16): the role they are learning, who trains them, and the
/// dates. A trainee's shifts in that role are never counted as coverage.
struct TraineeInfo: Codable, Equatable {
    var targetRole: String
    var trainer: String? = nil
    var from: String? = nil
    var until: String

    init(targetRole: String, trainer: String? = nil, from: String? = nil, until: String) {
        self.targetRole = targetRole; self.trainer = trainer; self.from = from; self.until = until
    }

    enum CodingKeys: String, CodingKey {
        case trainer, from, until
        case targetRole = "target_role"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        targetRole = c.setupText(.targetRole) ?? ""
        trainer = c.setupText(.trainer)
        from = c.setupText(.from)
        until = c.setupText(.until) ?? ""
    }

    /// "Learning Bartender with Jade until 11/15/26".
    var line: String {
        var s = "Learning \(targetRole)"
        if let trainer, !trainer.isEmpty { s += " with \(trainer)" }
        if let from, !until.isEmpty { s += ", " + CavnarDate.mdyRange(from, until) }
        else if !until.isEmpty { s += " until " + CavnarDate.mdy(until) }
        return s
    }
}

// MARK: - Who runs the floor, for the week (schedule_setup.manager_status)

struct ManagerStatus: Decodable, Equatable {
    struct Manager: Decodable, Equatable, Identifiable {
        var name: String
        var role: String?
        var basis: String?
        var why: String?
        var salaried: Bool
        var standingShifts: [StandingShift]
        var id: String { name }

        enum CodingKeys: String, CodingKey {
            case name, role, basis, why, salaried
            case standingShifts = "standing_shifts"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = c.setupText(.name) ?? ""
            role = c.setupText(.role)
            basis = c.setupText(.basis)
            why = c.setupText(.why)
            salaried = c.setupBool(.salaried) ?? false
            standingShifts = c.setupList(StandingShift.self, .standingShifts)
        }
    }

    struct Person: Decodable, Equatable, Identifiable {
        var name: String
        var role: String?
        var basis: String?
        var why: String?
        var id: String { name }

        enum CodingKeys: String, CodingKey { case name, role, basis, why }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = c.setupText(.name) ?? ""
            role = c.setupText(.role)
            basis = c.setupText(.basis)
            why = c.setupText(.why)
        }
    }

    struct Acting: Decodable, Equatable, Identifiable {
        var name: String
        var label: String?
        var id: String { name }

        enum CodingKeys: String, CodingKey { case name, label }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = c.setupText(.name) ?? ""
            label = c.setupText(.label)
        }
    }

    var managers: [Manager] = []
    var notCounted: [Person] = []
    var acting: [Acting] = []
    var line: String? = nil
    var askStanding: String? = nil
    var missingStanding: [String] = []

    enum CodingKeys: String, CodingKey {
        case managers, acting, line
        case notCounted = "not_counted"
        case askStanding = "ask_standing"
        case missingStanding = "missing_standing"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        managers = c.setupList(Manager.self, .managers).filter { !$0.name.isEmpty }
        notCounted = c.setupList(Person.self, .notCounted).filter { !$0.name.isEmpty }
        acting = c.setupList(Acting.self, .acting).filter { !$0.name.isEmpty }
        line = c.setupText(.line)
        askStanding = c.setupText(.askStanding)
        missingStanding = c.setupTexts(.missingStanding)
    }

    /// The salaried among the managers — what the salaried cap applies to.
    var salaried: [Manager] { managers.filter(\.salaried) }
}

// MARK: - Closers per role (schedule_setup.closer_review)

struct CloserReview: Decodable, Equatable {
    struct RoleGroup: Decodable, Equatable, Identifiable {
        var role: String
        var family: String?
        var closers: [String]
        var id: String { family ?? role }

        enum CodingKeys: String, CodingKey { case role, family, closers }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            role = c.setupText(.role) ?? ""
            family = c.setupText(.family)
            closers = c.setupTexts(.closers)
        }
    }

    /// keep / unmark / add, read from who was the last of their role out.
    struct Suggestion: Decodable, Equatable, Identifiable {
        var name: String
        var role: String?
        var closes: Int
        var action: String
        var reason: String?
        var id: String { name }

        enum CodingKeys: String, CodingKey { case name, role, closes, action, reason }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = c.setupText(.name) ?? ""
            role = c.setupText(.role)
            closes = c.setupInt(.closes) ?? 0
            action = c.setupText(.action) ?? "keep"
            reason = c.setupText(.reason)
        }
    }

    var byRole: [RoleGroup] = []
    var flagged: Int = 0
    var roster: Int = 0
    var share: Double = 0
    var warning: String? = nil
    var closerRoles: [String] = []
    var closerRolesBasis: String? = nil
    var closerRolesInForce: [String] = []
    var outsideRoles: [String] = []
    var pendingAdmin: [String] = []
    var suggestions: [Suggestion] = []
    var canEdit: Bool = false

    enum CodingKeys: String, CodingKey {
        case flagged, roster, share, warning, suggestions
        case byRole = "by_role"
        case closerRoles = "closer_roles"
        case closerRolesBasis = "closer_roles_basis"
        case closerRolesInForce = "closer_roles_in_force"
        case outsideRoles = "outside_roles"
        case pendingAdmin = "pending_admin"
        case canEdit = "can_edit"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        byRole = c.setupList(RoleGroup.self, .byRole).filter { !$0.role.isEmpty }
        flagged = c.setupInt(.flagged) ?? 0
        roster = c.setupInt(.roster) ?? 0
        share = c.setupDouble(.share) ?? 0
        warning = c.setupText(.warning)
        closerRoles = c.setupTexts(.closerRoles)
        closerRolesBasis = c.setupText(.closerRolesBasis)
        closerRolesInForce = c.setupTexts(.closerRolesInForce)
        outsideRoles = c.setupTexts(.outsideRoles)
        pendingAdmin = c.setupTexts(.pendingAdmin)
        suggestions = c.setupList(Suggestion.self, .suggestions).filter { !$0.name.isEmpty }
        canEdit = c.setupBool(.canEdit) ?? false
    }

    /// "The closer rule holds for Bartender, Barback — from your punches."
    var inForceLine: String? {
        guard !closerRolesInForce.isEmpty else { return nil }
        let roles = closerRolesInForce.joined(separator: ", ")
        switch closerRolesBasis {
        case "yours": return "The closer rule holds for \(roles) \u{2014} the roles you chose."
        case "history": return "The closer rule holds for \(roles) \u{2014} from your punches: the roles whose last person is out at close."
        default: return "The closer rule holds for \(roles) \u{2014} every role somebody is marked to close for."
        }
    }
}

// MARK: - Role families (schedule_setup.suggest_role_families)

struct RoleFamilies: Decodable, Equatable {
    struct Family: Decodable, Equatable, Identifiable {
        var family: String
        var label: String
        var roles: [String]
        var source: String?
        var id: String { family }

        enum CodingKeys: String, CodingKey { case family, label, roles, source }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            family = c.setupText(.family) ?? ""
            label = c.setupText(.label) ?? family
            roles = c.setupTexts(.roles)
            source = c.setupText(.source)
        }
    }

    var families: [Family] = []
    /// The owner's own map {job code: role}, as stored.
    var stored: [String: String] = [:]
    var roles: [String] = []
    var canEdit: Bool = false

    enum CodingKeys: String, CodingKey {
        case families, stored, roles
        case canEdit = "can_edit"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        families = c.setupList(Family.self, .families).filter { !$0.family.isEmpty }
        stored = ((try? c.decodeIfPresent([String: String].self, forKey: .stored)) ?? nil) ?? [:]
        roles = c.setupTexts(.roles)
        canEdit = c.setupBool(.canEdit) ?? false
    }

    /// The role each job code belongs to now: the owner's map, else the
    /// family it was grouped into.
    var assignment: [String: String] {
        var out: [String: String] = [:]
        for f in families { for r in f.roles { out[r] = f.label } }
        for (code, role) in stored { out[code] = role }
        return out
    }
}

// MARK: - Floors suggested from history (schedule_setup.suggest_role_floors)

struct FloorSuggestions: Decodable {
    var floors: [String: RoleFloor] = [:]
    var note: String? = nil

    enum CodingKeys: String, CodingKey { case floors, note }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        floors = ((try? c.decodeIfPresent([String: RoleFloor].self, forKey: .floors)) ?? nil) ?? [:]
        note = c.setupText(.note)
    }
}

/// A day where the floors ask for more people than there are dining
/// sections (schedule_engine.floor_cap_conflicts, P-29).
struct FloorCapConflict: Decodable, Equatable, Identifiable {
    var day: String
    var daypart: String
    var floor: Int
    var cap: Int
    var roles: [String]
    var id: String { "\(day)|\(daypart)" }

    enum CodingKeys: String, CodingKey { case day, daypart, floor, cap, roles }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = c.setupText(.day) ?? ""
        daypart = c.setupText(.daypart) ?? ""
        floor = c.setupInt(.floor) ?? 0
        cap = c.setupInt(.cap) ?? 0
        roles = c.setupTexts(.roles)
    }

    /// "Saturday dinner: your floors ask for 8 Server but you have 6
    /// sections — no schedule can hold both. Lower a floor or raise the
    /// section count."
    var sentence: String {
        let part = daypart == "morning" ? "lunch" : (daypart == "night" ? "dinner" : daypart)
        let who = roles.isEmpty ? "people" : roles.joined(separator: " and ")
        return "\(day) \(part): your floors ask for \(floor) \(who) but you have \(cap) sections \u{2014} no schedule "
            + "can hold both. Lower a floor or raise the section count."
    }
}

/// One role where the two stored close settings differ (D-43).
struct RoleCloseConflict: Decodable, Equatable, Identifiable {
    var role: String
    var stays: Int
    var mayRun: Int
    var id: String { role }

    enum CodingKeys: String, CodingKey {
        case role, stays
        case mayRun = "may_run"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        role = c.setupText(.role) ?? ""
        stays = c.setupInt(.stays) ?? 0
        mayRun = c.setupInt(.mayRun) ?? 0
    }

    /// "Bartender: stays 30, may run 60 — pick one."
    var sentence: String { "\(role): stays \(stays) min, may run \(mayRun) min \u{2014} pick one and save." }
}

/// A staffing rule the owner wrote, as the schedule checks it.
struct OwnerRuleReadback: Decodable, Equatable, Identifiable {
    var text: String?
    var readsAs: String?
    var id: String { (text ?? "") + "|" + (readsAs ?? "") }

    enum CodingKeys: String, CodingKey {
        case text
        case readsAs = "reads_as"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = c.setupText(.text)
        readsAs = c.setupText(.readsAs)
    }
}

// MARK: - Salaried staff (GET account/targets, D-7)

struct SalariedEntry: Decodable, Equatable, Identifiable {
    var name: String
    var annual: Double?
    var matched: String?
    var suggestion: String?
    var warning: String?
    var id: String { name }

    enum CodingKeys: String, CodingKey { case name, annual, matched, suggestion, warning }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.setupText(.name) ?? ""
        annual = c.setupDouble(.annual)
        matched = c.setupText(.matched)
        suggestion = c.setupText(.suggestion)
        warning = c.setupText(.warning)
    }
}

// MARK: - Labor standards (labor_standards.standards, D-25)

struct LaborStandard: Decodable, Equatable {
    var perHour: Double?
    var unit: String?
    var source: String?
    var measured: Double?
    var days: Int?
    var text: String?

    enum CodingKeys: String, CodingKey {
        case unit, source, measured, days, text
        case perHour = "per_hour"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        perHour = c.setupDouble(.perHour)
        unit = c.setupText(.unit)
        source = c.setupText(.source)
        measured = c.setupDouble(.measured)
        days = c.setupInt(.days)
        text = c.setupText(.text)
    }

    var isYours: Bool { source == "yours" }
}

struct LaborStandardsPayload: Decodable {
    /// {family: {"morning" | "night": standard}}.
    var standards: [String: [String: LaborStandard]] = [:]
    var families: [String] = []
    var units: [String: String] = [:]
    var canEdit: Bool = false

    enum CodingKeys: String, CodingKey {
        case standards, families, units
        case canEdit = "can_edit"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let raw = (try? c.decodeIfPresent([String: [String: LaborStandard]].self, forKey: .standards)) ?? nil {
            standards = raw
        }
        families = c.setupTexts(.families)
        units = ((try? c.decodeIfPresent([String: String].self, forKey: .units)) ?? nil) ?? [:]
        canEdit = c.setupBool(.canEdit) ?? false
    }
}

// MARK: - The week's money and hours (labor.week_hours_plan, D-1/D-2/E-24)

/// What the hours budget is: the all-in labor target less the salaried
/// staff's pay for the week, the wage it is bought at, and — when part of
/// that wage is assumed — the caveat. Salaried dollars ride only for the
/// owner (the server leaves them out for anyone else).
struct BudgetBasis: Codable, Equatable {
    var kind: String? = nil
    var targetPct: Double? = nil
    var rate: Double? = nil
    var caveat: String? = nil
    var trimOK: Bool? = nil
    var salariedPeople: Int? = nil
    var salariedWeekCost: Double? = nil
    var targetDollars: Double? = nil
    var salariesExceedTarget: Bool? = nil
    var text: String? = nil

    enum CodingKeys: String, CodingKey {
        case kind, rate, caveat, text
        case targetPct = "target_pct"
        case trimOK = "trim_ok"
        case salariedPeople = "salaried_people"
        case salariedWeekCost = "salaried_week_cost"
        case targetDollars = "target_dollars"
        case salariesExceedTarget = "salaries_exceed_target"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.setupText(.kind)
        targetPct = c.setupDouble(.targetPct)
        rate = c.setupDouble(.rate)
        caveat = c.setupText(.caveat)
        trimOK = c.setupBool(.trimOK)
        salariedPeople = c.setupInt(.salariedPeople)
        salariedWeekCost = c.setupDouble(.salariedWeekCost)
        targetDollars = c.setupDouble(.targetDollars)
        salariesExceedTarget = c.setupBool(.salariesExceedTarget)
        text = c.setupText(.text)
    }

    /// True when the budget is the target less salaries — the hourly crew's.
    var isNetOfSalaries: Bool { kind == "all_in_less_salaries" }
}

/// A lenient wrapper for a type whose Decodable is synthesized: an odd
/// shape reads as no basis instead of failing the payload around it.
struct LenientBudgetBasis: Codable, Equatable {
    let value: BudgetBasis?

    init(_ value: BudgetBasis?) { self.value = value }

    init(from decoder: Decoder) throws {
        value = try? BudgetBasis(from: decoder)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(value)
    }
}

/// GET labor/schedule-forecast — the week before anything is drafted:
/// each day's forecast sales and the hours the draft will be given.
struct ForecastPreview: Decodable {
    struct Day: Decodable, Identifiable {
        struct Demand: Decodable {
            var pct: Double?
            var reasons: [String]

            enum CodingKeys: String, CodingKey { case pct, reasons }

            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                pct = c.setupDouble(.pct)
                reasons = c.setupTexts(.reasons)
            }
        }

        var date: String
        var weekday: String
        var hours: Double?
        var closed: Bool
        var demand: Demand?
        var sales: Double?
        var low: Double?
        var high: Double?
        var effects: [String]
        var reason: String?
        var id: String { date }

        enum CodingKeys: String, CodingKey { case date, weekday, hours, closed, demand, sales, low, high, effects, reason }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            date = c.setupText(.date) ?? ""
            weekday = c.setupText(.weekday) ?? ""
            hours = c.setupDouble(.hours)
            closed = c.setupBool(.closed) ?? false
            demand = (try? c.decodeIfPresent(Demand.self, forKey: .demand)) ?? nil
            sales = c.setupDouble(.sales)
            low = c.setupDouble(.low)
            high = c.setupDouble(.high)
            effects = c.setupTexts(.effects)
            reason = c.setupText(.reason)
        }
    }

    struct DataThrough: Decodable {
        var line: String?
        var message: String?
        var blocked: Bool

        enum CodingKeys: String, CodingKey { case line, message, blocked }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            line = c.setupText(.line)
            message = c.setupText(.message)
            blocked = c.setupBool(.blocked) ?? false
        }
    }

    var ok: Bool = false
    var available: Bool = false
    var reason: String? = nil
    var error: String? = nil
    var weekStart: String? = nil
    var days: [Day] = []
    var projectedRevenue: Double? = nil
    var projectedRevenueSource: String? = nil
    var hoursBudget: Double? = nil
    var laborBudgetDollars: Double? = nil
    var laborTarget: Double? = nil
    var laborTargetLabel: String? = nil
    var budgetBasis: BudgetBasis? = nil
    var dailyTargetReasons: [String: [String]] = [:]
    var dataThrough: DataThrough? = nil

    enum CodingKeys: String, CodingKey {
        case ok, available, reason, error, days
        case weekStart = "week_start"
        case projectedRevenue = "projected_revenue"
        case projectedRevenueSource = "projected_revenue_source"
        case hoursBudget = "hours_budget"
        case laborBudgetDollars = "labor_budget_dollars"
        case laborTarget = "labor_target"
        case laborTargetLabel = "labor_target_label"
        case budgetBasis = "budget_basis"
        case dailyTargetReasons = "daily_target_reasons"
        case dataThrough = "demand_data_through"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.setupBool(.ok) ?? false
        available = c.setupBool(.available) ?? false
        reason = c.setupText(.reason)
        error = c.setupText(.error)
        weekStart = c.setupText(.weekStart)
        days = c.setupList(Day.self, .days).filter { !$0.date.isEmpty }
        projectedRevenue = c.setupDouble(.projectedRevenue)
        projectedRevenueSource = c.setupText(.projectedRevenueSource)
        hoursBudget = c.setupDouble(.hoursBudget)
        laborBudgetDollars = c.setupDouble(.laborBudgetDollars)
        laborTarget = c.setupDouble(.laborTarget)
        laborTargetLabel = c.setupText(.laborTargetLabel)
        budgetBasis = (try? c.decodeIfPresent(BudgetBasis.self, forKey: .budgetBasis)) ?? nil
        dailyTargetReasons = ((try? c.decodeIfPresent([String: [String]].self, forKey: .dailyTargetReasons)) ?? nil) ?? [:]
        dataThrough = (try? c.decodeIfPresent(DataThrough.self, forKey: .dataThrough)) ?? nil
    }
}

// MARK: - Words shared by the setup screens

enum SetupWords {
    /// "lunch" / "dinner" for the schedule's two dayparts.
    static func daypart(_ part: String?) -> String {
        switch part {
        case "morning": return "lunch"
        case "night": return "dinner"
        default: return part ?? ""
        }
    }

    /// "+30%" / "−15%" — a signed whole percent, the minus a real minus.
    static func signedPct(_ v: Double) -> String {
        let n = Int(v.rounded())
        return n > 0 ? "+\(n)%" : (n < 0 ? "\u{2212}\(abs(n))%" : "0%")
    }

    /// "$1,234" — whole dollars.
    static func dollars(_ v: Double) -> String { "$" + v.commaFormatted }

    /// The house time ("6:00pm") for minutes past midnight.
    static func time(minutes: Int) -> String {
        let m = ((minutes % 1440) + 1440) % 1440
        let h = m / 60, mm = m % 60
        let h12 = h % 12 == 0 ? 12 : h % 12
        return "\(h12):\(String(format: "%02d", mm))\(h < 12 ? "am" : "pm")"
    }

    /// Minutes past midnight for "6:00pm", "6pm", "18:00"; nil otherwise.
    static func minutes(_ raw: String?) -> Int? {
        guard var s = raw?.trimmingCharacters(in: .whitespaces).lowercased(), !s.isEmpty else { return nil }
        var meridiem: String?
        for m in ["am", "pm"] where s.hasSuffix(m) {
            meridiem = m
            s = String(s.dropLast(2)).trimmingCharacters(in: .whitespaces)
        }
        let parts = s.split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 1 || parts.count == 2, var h = Int(parts[0]) else { return nil }
        let mm = parts.count == 2 ? Int(parts[1]) : 0
        guard let mm, (0..<60).contains(mm) else { return nil }
        if let meridiem {
            guard (1...12).contains(h) else { return nil }
            if meridiem == "am" { h = h == 12 ? 0 : h } else { h = h == 12 ? 12 : h + 12 }
        } else {
            guard (0..<24).contains(h) else { return nil }
        }
        return h * 60 + mm
    }

    /// Every quarter hour of the day, in the house form — the iOS twin of
    /// the web's `cavTimeOptions`.
    static let quarterHours: [String] = (0..<96).map { time(minutes: $0 * 15) }
}
