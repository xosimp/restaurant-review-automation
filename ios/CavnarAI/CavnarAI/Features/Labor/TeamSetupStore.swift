import Foundation
import Observation

/// What GET labor/rules adds for the owner to confirm (schedule audit
/// 10/3/26 F1, A1, A2, F2): who runs the floor, the closers per role, each
/// staffing rule as it is checked, trading days with no close time, the one
/// stays-after-close setting per role, the salaried cap, and the floors that
/// ask for more people than there are sections. Decoded from the same
/// payload as the rules themselves (ScheduleSetupViewModel.loadRules).
struct RulesSetupFields: Decodable {
    var managers: ManagerStatus? = nil
    var managersLine: String? = nil
    var closers: CloserReview? = nil
    var ownerRules: [OwnerRuleReadback] = []
    var ownerRulesUnchecked: [String] = []
    /// The RESTAURANT HOURS & SHIFT RULES as every draft is checked against
    /// them, and the lines the schedule can't read (PROMPT-1).
    var hoursRules: [OwnerRuleReadback] = []
    var hoursRulesUnchecked: [String] = []
    var closeTimesMissing: [String] = []
    var roleCloseMins: [String: Int] = [:]
    var roleCloseConflicts: [RoleCloseConflict] = []
    var salariedCap: Double? = nil
    var salariedCapDefault: Double? = nil
    var salariedCapBounds: [Double] = []
    var closerRoles: [String] = []
    var floorCapConflicts: [FloorCapConflict] = []
    var certificationLabels: [String: String] = [:]
    var sectionCount: Int? = nil
    var crossTrainingDefaults: [String: Int] = [:]
    /// Each salaried person and the weekly cap code holds them to — the
    /// owner's alone (schedule_setup.salaried_caps, E-12/E-17).
    var salariedCaps: [SalariedCap] = []

    enum CodingKeys: String, CodingKey {
        case managers, closers
        case salariedCaps = "salaried_caps"
        case managersLine = "managers_line"
        case ownerRules = "owner_rules"
        case ownerRulesUnchecked = "owner_rules_unchecked"
        case hoursRules = "hours_rules"
        case hoursRulesUnchecked = "hours_rules_unchecked"
        case closeTimesMissing = "close_times_missing"
        case roleCloseMins = "role_close_mins"
        case roleCloseConflicts = "role_close_conflicts"
        case salariedCap = "salaried_cap"
        case salariedCapDefault = "salaried_cap_default"
        case salariedCapBounds = "salaried_cap_bounds"
        case closerRoles = "closer_roles"
        case floorCapConflicts = "floor_cap_conflicts"
        case certificationLabels = "certification_labels"
        case sectionCount = "section_count"
        case crossTrainingDefaults = "cross_training_defaults"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        managers = (try? c.decodeIfPresent(ManagerStatus.self, forKey: .managers)) ?? nil
        managersLine = c.setupText(.managersLine)
        closers = (try? c.decodeIfPresent(CloserReview.self, forKey: .closers)) ?? nil
        ownerRules = c.setupList(OwnerRuleReadback.self, .ownerRules)
        ownerRulesUnchecked = c.setupTexts(.ownerRulesUnchecked)
        hoursRules = c.setupList(OwnerRuleReadback.self, .hoursRules)
        hoursRulesUnchecked = c.setupTexts(.hoursRulesUnchecked)
        closeTimesMissing = c.setupTexts(.closeTimesMissing)
        roleCloseMins = ((try? c.decodeIfPresent([String: Double].self, forKey: .roleCloseMins)) ?? nil)?
            .compactMapValues { $0.isFinite ? Int($0.rounded()) : nil } ?? [:]
        roleCloseConflicts = c.setupList(RoleCloseConflict.self, .roleCloseConflicts).filter { !$0.role.isEmpty }
        salariedCap = c.setupDouble(.salariedCap)
        salariedCapDefault = c.setupDouble(.salariedCapDefault)
        salariedCapBounds = ((try? c.decodeIfPresent([Double].self, forKey: .salariedCapBounds)) ?? nil) ?? []
        closerRoles = c.setupTexts(.closerRoles)
        floorCapConflicts = c.setupList(FloorCapConflict.self, .floorCapConflicts)
        certificationLabels = ((try? c.decodeIfPresent([String: String].self, forKey: .certificationLabels)) ?? nil) ?? [:]
        sectionCount = c.setupInt(.sectionCount)
        crossTrainingDefaults = ((try? c.decodeIfPresent([String: Double].self, forKey: .crossTrainingDefaults)) ?? nil)?
            .compactMapValues { $0.isFinite ? Int($0.rounded()) : nil } ?? [:]
        salariedCaps = c.setupList(SalariedCap.self, .salariedCaps).filter { !$0.name.isEmpty }
    }
}

/// One salaried person and the most hours a week code schedules them for:
/// their own maximum (`own`), else the salaried cap.
struct SalariedCap: Decodable, Equatable, Identifiable {
    var name: String
    var cap: Double?
    var own: Bool
    var id: String { name }

    enum CodingKeys: String, CodingKey { case name, cap, own }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.setupText(.name) ?? ""
        cap = c.setupDouble(.cap)
        own = c.setupBool(.own) ?? false
    }
}

/// The setup the owner confirms around the roster and the rules — the
/// managers, the closers, the role families, floors from history, the
/// labor standards, the salaried list and next week's forecast. One store
/// held by ScheduleSetupViewModel (`teamSetup`), so the rules sheet, the
/// person sheet and the setup sections read the same state.
@Observable
@MainActor
final class TeamSetupStore {
    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    // MARK: From the rules

    var managers: ManagerStatus?
    var managersLine: String?
    var closerSummary: CloserReview?
    var ownerRules: [OwnerRuleReadback] = []
    var ownerRulesUnchecked: [String] = []
    var hoursRules: [OwnerRuleReadback] = []
    var hoursRulesUnchecked: [String] = []
    var closeTimesMissing: [String] = []
    var roleCloseMins: [String: Int] = [:]
    var roleCloseConflicts: [RoleCloseConflict] = []
    var salariedCap: Double?
    var salariedCapDefault: Double = 55
    var salariedCapBounds: ClosedRange<Double> = 20...84
    var floorCapConflicts: [FloorCapConflict] = []
    var certificationLabels: [String: String] = [:]
    var sectionCount: Int?
    /// The roster's roles (the cross-training defaults are keyed by them).
    var rosterRoles: [String] = []
    var salariedCaps: [SalariedCap] = []

    func apply(_ f: RulesSetupFields) {
        managers = f.managers
        managersLine = f.managersLine ?? f.managers?.line
        closerSummary = f.closers
        ownerRules = f.ownerRules
        ownerRulesUnchecked = f.ownerRulesUnchecked
        hoursRules = f.hoursRules
        hoursRulesUnchecked = f.hoursRulesUnchecked
        closeTimesMissing = f.closeTimesMissing
        roleCloseMins = f.roleCloseMins
        roleCloseConflicts = f.roleCloseConflicts
        salariedCap = f.salariedCap
        if let d = f.salariedCapDefault { salariedCapDefault = d }
        if f.salariedCapBounds.count == 2, f.salariedCapBounds[0] <= f.salariedCapBounds[1] {
            salariedCapBounds = f.salariedCapBounds[0]...f.salariedCapBounds[1]
        }
        floorCapConflicts = f.floorCapConflicts
        certificationLabels = f.certificationLabels
        sectionCount = f.sectionCount
        rosterRoles = f.crossTrainingDefaults.keys.sorted()
        salariedCaps = f.salariedCaps
    }

    // MARK: Who runs the floor (POST labor/managers)

    var managerBusy: String?
    var managerError: String?

    private struct ManagerBody: Encodable {
        let name: String
        let choice: ScheduleSetupViewModel.StaffSettingsPatch.FloorManagerChoice
        enum CodingKeys: String, CodingKey { case name, floorManager = "floor_manager" }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(name, forKey: .name)
            switch choice {
            case .yes: try c.encode(true, forKey: .floorManager)
            case .no: try c.encode(false, forKey: .floorManager)
            case .automatic: try c.encode("auto", forKey: .floorManager)
            }
        }
    }

    private struct ManagerResponse: Decodable {
        let ok: Bool
        let error: String?
        let status: ManagerStatus?
        enum CodingKeys: String, CodingKey { case ok, error }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = c.setupBool(.ok) ?? false
            error = c.setupText(.error)
            status = ok ? (try? ManagerStatus(from: decoder)) : nil
        }
    }

    /// The owner's word on who runs the floor — yes, no or automatic. The
    /// answer is the week's managers as the rules now read them.
    @discardableResult
    func setFloorManager(_ name: String,
                         _ choice: ScheduleSetupViewModel.StaffSettingsPatch.FloorManagerChoice) async -> Bool {
        managerBusy = name
        managerError = nil
        defer { managerBusy = nil }
        do {
            let r: ManagerResponse = try await client.send(
                "/mobile/api/labor/managers", method: .post, body: ManagerBody(name: name, choice: choice),
                hapticOnError: false, retryTransient: false)
            guard r.ok else { managerError = r.error ?? "Couldn\u{2019}t change that."; return false }
            if let s = r.status {
                managers = s
                managersLine = s.line
            }
            Haptic.light()
            return true
        } catch let error as APIClient.APIError {
            managerError = error.message
        } catch {
            managerError = "Couldn\u{2019}t change that."
        }
        Haptic.error()
        return false
    }

    // MARK: Closers (GET / POST labor/closers)

    var closerReview: CloserReview?
    var isLoadingClosers = false
    var closerError: String?
    var isApplyingClosers = false
    /// "Applied — 6 changes." after an Apply.
    var closerMessage: String?

    struct CloserChange: Encodable, Equatable {
        let name: String
        var canClose: Bool? = nil
        var closesFor: [String]? = nil
        enum CodingKeys: String, CodingKey {
            case name
            case canClose = "can_close"
            case closesFor = "closes_for"
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(name, forKey: .name)
            try c.encodeIfPresent(canClose, forKey: .canClose)
            try c.encodeIfPresent(closesFor, forKey: .closesFor)
        }
    }

    private struct ClosersBody: Encodable {
        let changes: [CloserChange]
        let closerRoles: [String]?
        enum CodingKeys: String, CodingKey {
            case changes
            case closerRoles = "closer_roles"
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(changes, forKey: .changes)
            try c.encodeIfPresent(closerRoles, forKey: .closerRoles)
        }
    }

    private struct ClosersResponse: Decodable {
        let ok: Bool
        let error: String?
        let applied: Int?
        let review: CloserReview?
        enum CodingKeys: String, CodingKey { case ok, error, applied }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = c.setupBool(.ok) ?? false
            error = c.setupText(.error)
            applied = c.setupInt(.applied)
            review = ok ? (try? CloserReview(from: decoder)) : nil
        }
    }

    func loadClosers() async {
        isLoadingClosers = closerReview == nil
        defer { isLoadingClosers = false }
        do {
            let r: ClosersResponse = try await client.send("/mobile/api/labor/closers", hapticOnError: false)
            guard r.ok else { closerError = r.error ?? "Couldn\u{2019}t read the closers."; return }
            closerReview = r.review
            closerError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if closerReview == nil { closerError = error.message }
        } catch {
            if closerReview == nil { closerError = "Couldn\u{2019}t read the closers." }
        }
    }

    /// The owner's cleanup, applied as chosen: each person marked or
    /// unmarked to close, the roles they close for, and which roles have
    /// closers. Nothing is applied that the owner did not pick.
    @discardableResult
    func applyClosers(_ changes: [CloserChange], closerRoles: [String]?) async -> Bool {
        isApplyingClosers = true
        closerError = nil
        closerMessage = nil
        defer { isApplyingClosers = false }
        do {
            let r: ClosersResponse = try await client.send(
                "/mobile/api/labor/closers", method: .post, body: ClosersBody(changes: changes, closerRoles: closerRoles),
                hapticOnError: false, retryTransient: false)
            guard r.ok else {
                closerError = r.error ?? "Couldn\u{2019}t apply that."
                if let n = r.applied, n > 0 { closerError! += " \(n) of the changes went through first." }
                return false
            }
            if let review = r.review { closerReview = review; closerSummary = review }
            let n = r.applied ?? changes.count
            closerMessage = n == 0 ? "Closer roles saved." : "Applied \u{2014} \(n) change\(n == 1 ? "" : "s")."
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            closerError = error.message
        } catch {
            closerError = "Couldn\u{2019}t apply that."
        }
        Haptic.error()
        return false
    }

    // MARK: Ratings and closer flags entered through support

    var isAdopting = false
    var adoptMessage: String?
    var adoptError: String?

    private struct AdoptResponse: Decodable {
        let ok: Bool
        let error: String?
        let adopted: Int?
        enum CodingKeys: String, CodingKey { case ok, error, adopted }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = c.setupBool(.ok) ?? false
            error = c.setupText(.error)
            adopted = c.setupInt(.adopted)
        }
    }

    private struct EmptyBody: Encodable {}

    /// "Count them as mine": the ratings and closer flags entered through
    /// Cavnar AI support count as the account holder's (models.
    /// adopt_admin_ratings — principal only, never through view-as).
    @discardableResult
    func adoptSupportRatings() async -> Bool {
        isAdopting = true
        adoptError = nil
        adoptMessage = nil
        defer { isAdopting = false }
        do {
            let r: AdoptResponse = try await client.send(
                "/mobile/api/labor/team/ratings/adopt", method: .post, body: EmptyBody(),
                hapticOnError: false, retryTransient: false)
            guard r.ok else { adoptError = r.error ?? "Couldn\u{2019}t count those as yours."; return false }
            let n = r.adopted ?? 0
            adoptMessage = n == 0 ? "Nothing was waiting." : "Counted as yours \u{2014} \(n) rating\(n == 1 ? "" : "s") and flags."
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            adoptError = error.message
        } catch {
            adoptError = "Couldn\u{2019}t count those as yours."
        }
        Haptic.error()
        return false
    }

    // MARK: Role families (GET / POST labor/role-families)

    var families: RoleFamilies?
    var isLoadingFamilies = false
    var isSavingFamilies = false
    var familiesError: String?

    private struct FamiliesResponse: Decodable {
        let ok: Bool
        let error: String?
        let families: RoleFamilies?
        enum CodingKeys: String, CodingKey { case ok, error }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = c.setupBool(.ok) ?? false
            error = c.setupText(.error)
            families = ok ? (try? RoleFamilies(from: decoder)) : nil
        }
    }

    private struct FamiliesBody: Encodable { let families: [String: String] }

    func loadFamilies() async {
        isLoadingFamilies = families == nil
        defer { isLoadingFamilies = false }
        do {
            let r: FamiliesResponse = try await client.send("/mobile/api/labor/role-families", hapticOnError: false)
            guard r.ok else { familiesError = r.error ?? "Couldn\u{2019}t read the roles."; return }
            families = r.families
            familiesError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if families == nil { familiesError = error.message }
        } catch {
            if families == nil { familiesError = "Couldn\u{2019}t read the roles." }
        }
    }

    /// The owner's whole map {job code: role} — {} goes back to the
    /// suggestion.
    @discardableResult
    func saveFamilies(_ map: [String: String]) async -> Bool {
        isSavingFamilies = true
        familiesError = nil
        defer { isSavingFamilies = false }
        do {
            let r: FamiliesResponse = try await client.send(
                "/mobile/api/labor/role-families", method: .post, body: FamiliesBody(families: map),
                hapticOnError: false, retryTransient: false)
            guard r.ok else { familiesError = r.error ?? "Couldn\u{2019}t save the roles."; return false }
            if let f = r.families { families = f }
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            familiesError = error.message
        } catch {
            familiesError = "Couldn\u{2019}t save the roles."
        }
        Haptic.error()
        return false
    }

    // MARK: Floors from history (GET labor/floors/suggest)

    var isSuggestingFloors = false
    var floorSuggestError: String?

    private struct FloorSuggestResponse: Decodable {
        let ok: Bool
        let error: String?
        let suggestions: FloorSuggestions?
        enum CodingKeys: String, CodingKey { case ok, error }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = c.setupBool(.ok) ?? false
            error = c.setupText(.error)
            suggestions = ok ? (try? FloorSuggestions(from: decoder)) : nil
        }
    }

    /// "Never below" per role and daypart from the last eight weeks —
    /// nothing is saved here; the rules sheet's Save stores what the owner
    /// keeps.
    func suggestFloors() async -> FloorSuggestions? {
        isSuggestingFloors = true
        floorSuggestError = nil
        defer { isSuggestingFloors = false }
        do {
            let r: FloorSuggestResponse = try await client.send("/mobile/api/labor/floors/suggest", hapticOnError: false)
            guard r.ok, let s = r.suggestions else {
                floorSuggestError = r.error ?? "Couldn\u{2019}t read your history."
                return nil
            }
            if s.floors.isEmpty {
                floorSuggestError = "Not enough shift history yet to suggest floors."
                return nil
            }
            return s
        } catch let error as APIClient.APIError {
            floorSuggestError = error.message
        } catch {
            floorSuggestError = "Couldn\u{2019}t read your history."
        }
        return nil
    }

    // MARK: Labor standards (GET / POST labor/labor-standards)

    var standards: LaborStandardsPayload?
    var isLoadingStandards = false
    var standardsError: String?
    var standardBusy: String?

    private struct StandardsResponse: Decodable {
        let ok: Bool
        let error: String?
        let payload: LaborStandardsPayload?
        enum CodingKeys: String, CodingKey { case ok, error }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = c.setupBool(.ok) ?? false
            error = c.setupText(.error)
            payload = ok ? (try? LaborStandardsPayload(from: decoder)) : nil
        }
    }

    private struct StandardBody: Encodable {
        let family: String
        var lunch: Double? = nil
        var dinner: Double? = nil
        var remove: Bool? = nil
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(family, forKey: .family)
            try c.encodeIfPresent(lunch, forKey: .lunch)
            try c.encodeIfPresent(dinner, forKey: .dinner)
            try c.encodeIfPresent(remove, forKey: .remove)
        }
        enum CodingKeys: String, CodingKey { case family, lunch, dinner, remove }
    }

    func loadStandards() async {
        isLoadingStandards = standards == nil
        defer { isLoadingStandards = false }
        do {
            let r: StandardsResponse = try await client.send("/mobile/api/labor/labor-standards", hapticOnError: false)
            guard r.ok else { standardsError = r.error ?? "Couldn\u{2019}t read the labor standards."; return }
            standards = r.payload
            standardsError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if standards == nil { standardsError = error.message }
        } catch {
            if standards == nil { standardsError = "Couldn\u{2019}t read the labor standards." }
        }
    }

    /// One family's own standard — lunch, dinner or both — or `remove` to
    /// go back to what was measured. One family at a time: nothing else
    /// stored is sent back.
    @discardableResult
    func setStandard(family: String, lunch: Double?, dinner: Double?, remove: Bool = false) async -> Bool {
        standardBusy = family
        standardsError = nil
        defer { standardBusy = nil }
        do {
            let body = StandardBody(family: family, lunch: remove ? nil : lunch, dinner: remove ? nil : dinner,
                                    remove: remove ? true : nil)
            let r: StandardsResponse = try await client.send(
                "/mobile/api/labor/labor-standards", method: .post, body: body,
                hapticOnError: false, retryTransient: false)
            guard r.ok else { standardsError = r.error ?? "Couldn\u{2019}t save that standard."; return false }
            // The POST answers the standards alone; the families and units
            // stay as the GET gave them.
            if let p = r.payload {
                var next = standards ?? LaborStandardsPayload()
                next.standards = p.standards
                standards = next
            }
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            standardsError = error.message
        } catch {
            standardsError = "Couldn\u{2019}t save that standard."
        }
        Haptic.error()
        return false
    }

    // MARK: Next week's forecast (GET labor/schedule-forecast)

    var forecast: ForecastPreview?
    var isLoadingForecast = false
    var forecastError: String?

    func loadForecast() async {
        isLoadingForecast = forecast == nil
        defer { isLoadingForecast = false }
        do {
            let r: ForecastPreview = try await client.send("/mobile/api/labor/schedule-forecast", hapticOnError: false)
            guard r.ok else { forecastError = r.error ?? "Couldn\u{2019}t read next week\u{2019}s forecast."; return }
            forecast = r
            forecastError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if forecast == nil { forecastError = error.message }
        } catch {
            if forecast == nil { forecastError = "Couldn\u{2019}t read next week\u{2019}s forecast." }
        }
    }

    // MARK: Salaried staff (GET / POST account/targets — the owner's only)

    var salaried: [SalariedEntry] = []
    var salariedLoaded = false
    var salariedBusy: String?
    var salariedError: String?

    private struct TargetsResponse: Decodable {
        struct Targets: Decodable {
            var salaried: [SalariedEntry]
            enum CodingKeys: String, CodingKey { case salaried }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                salaried = c.setupList(SalariedEntry.self, .salaried).filter { !$0.name.isEmpty }
            }
        }
        let ok: Bool
        let error: String?
        let targets: Targets?
        enum CodingKeys: String, CodingKey { case ok, error, targets }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = c.setupBool(.ok) ?? false
            error = c.setupText(.error)
            targets = (try? c.decodeIfPresent(Targets.self, forKey: .targets)) ?? nil
        }
    }

    private struct SalariedAdd: Encodable { let name: String; let annual: Double }
    private struct LinkBody: Encodable {
        let salariedRemove: String
        let salariedAdd: SalariedAdd
        enum CodingKeys: String, CodingKey {
            case salariedRemove = "salaried_remove"
            case salariedAdd = "salaried_add"
        }
    }

    /// The salaried people (the account holder's alone — another login is
    /// sent none) and whether each matches somebody on the roster.
    func loadSalaried() async {
        do {
            let r: TargetsResponse = try await client.send("/mobile/api/account/targets", hapticOnError: false)
            guard r.ok else { return }
            salaried = r.targets?.salaried ?? []
            salariedLoaded = true
        } catch {
            // A secondary list on the rules sheet; the rest stands without it.
        }
    }

    /// "Link to Gabe Huerta": the entry is saved again under the roster's
    /// spelling (one add and one remove in a single save, so the salary is
    /// never counted twice or dropped).
    @discardableResult
    func linkSalaried(_ entry: SalariedEntry, to name: String) async -> Bool {
        guard let annual = entry.annual, annual > 0 else {
            salariedError = "That salary isn\u{2019}t on file here \u{2014} link it on the web, in Account \u{2192} Targets."
            return false
        }
        salariedBusy = entry.name
        salariedError = nil
        defer { salariedBusy = nil }
        do {
            let r: APIClient.OKResponse = try await client.send(
                "/mobile/api/account/targets", method: .post,
                body: LinkBody(salariedRemove: entry.name, salariedAdd: SalariedAdd(name: name, annual: annual)),
                hapticOnError: false, retryTransient: false)
            guard r.ok else { salariedError = r.error ?? "Couldn\u{2019}t link that."; return false }
            Haptic.success()
            await loadSalaried()
            return true
        } catch let error as APIClient.APIError {
            salariedError = error.message
        } catch {
            salariedError = "Couldn\u{2019}t link that."
        }
        Haptic.error()
        return false
    }
}
