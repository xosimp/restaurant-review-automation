import SwiftUI
import Observation

/// Account → Targets & pay rates (iOS parity #27): the web's card of the
/// same name on the same /account/targets twin (strategy_routes
/// ._do_targets_get / _do_targets_set). Labor, food-cost and waste targets,
/// the revenue target by the month or by the week, the blended rate, the
/// day the payroll week starts, pay by role and by person, and the salaried
/// staff. Every field saves on its own when it's left — one field per save,
/// as on the web — and a salaried person is added or removed one at a time
/// (owner edits never vanish). A login that may not change them reads them;
/// one without pay access is sent no wages at all.
struct AccountTargetsView: View {
    @State private var model = AccountTargetsModel()
    @FocusState private var focus: String?
    @State private var pendingSalariedRemove: SalariedEntry?
    @State private var newSalariedName = ""
    @State private var newSalariedAnnual = ""
    @State private var openRoles: Set<String> = []

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Targets & pay rates") {
                        GlowBadge(systemImage: "target", size: 64)
                    } subtitle: {
                        Text("What every module judges against")
                    }

                    if model.isLoading && model.payload == nil {
                        CavnarSkeletonBar(height: 3)
                            .padding(.vertical, 10)
                            .accessibilityLabel("Loading your targets")
                    } else if let p = model.payload {
                        if !model.canEdit {
                            CavnarCaveat(title: "Only the account owner can change these",
                                         detail: "These are the targets and rates your restaurant uses.")
                        }
                        targetsSection(p)
                        revenueSection(p)
                        if model.seesPay { paySection(p) }
                        if model.canEdit { salariedSection(p) }
                    }
                    if let error = model.error {
                        Text(error).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
                .padding(.bottom, focus == nil ? 0 : 260)
            }
            .scrollDismissesKeyboard(.interactively)
            .accountSheetChrome("Targets")
            .keyboardDoneToolbar { focus = nil }
            .cavnarPostedOverlay(model.posted) { model.posted = nil }
            .cavnarEmberRefreshable { await model.load() }
            .task { await model.load() }
            // Leaving a field saves it — that field only.
            .onChange(of: focus) { old, _ in
                if let old { Task { await model.commit(old) } }
            }
            .confirmationDialog(
                pendingSalariedRemove.map { "Remove \($0.name)?" } ?? "",
                isPresented: Binding(get: { pendingSalariedRemove != nil }, set: { if !$0 { pendingSalariedRemove = nil } }),
                titleVisibility: .visible
            ) {
                Button("Remove from salaried", role: .destructive) {
                    guard let entry = pendingSalariedRemove else { return }
                    Task { await model.removeSalaried(entry.name) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Their hours are costed by the hour again, from their rate.")
            }
        }
    }

    // MARK: Targets

    private func targetsSection(_ p: TargetsPayload) -> some View {
        AccountSection(kicker: "Targets") {
            numberRow("Labor target", key: TargetsPayload.Field.labor, suffix: "%",
                      note: p.noteFor(TargetsPayload.Field.labor))
            numberRow("Food cost target", key: TargetsPayload.Field.food, suffix: "%",
                      note: p.noteFor(TargetsPayload.Field.food))
            numberRow("Waste target", key: TargetsPayload.Field.waste, suffix: "%",
                      placeholder: "Not set", note: p.noteFor(TargetsPayload.Field.waste))
            AccountKVRow(label: "Payroll week starts", showsDivider: false) {
                Picker("Payroll week starts", selection: Binding(
                    get: { model.payload?.weekStartDay ?? 0 },
                    set: { day in Haptic.selection(); Task { await model.setWeekStart(day) } }
                )) {
                    ForEach(Array(TargetsPayload.weekdays.enumerated()), id: \.offset) { i, d in
                        Text(d).tag(i)
                    }
                }
                .labelsHidden().tint(Color.cavnarEmber)
                .disabled(!model.canEdit)
            }
        }
    }

    private func revenueSection(_ p: TargetsPayload) -> some View {
        AccountSection(kicker: "Revenue target") {
            numberRow("By the month", key: TargetsPayload.Field.monthly, prefix: "$",
                      note: p.noteFor(TargetsPayload.Field.monthly))
            numberRow("By the week", key: TargetsPayload.Field.weekly, prefix: "$",
                      note: p.noteFor(TargetsPayload.Field.weekly))
            Text("One target: typing either fills the other (a month is 52 ÷ 12 weeks). Only the one you typed is saved.")
                .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.vertical, 6)
        }
    }

    // MARK: Pay

    private func paySection(_ p: TargetsPayload) -> some View {
        AccountSection(kicker: "Pay rates") {
            numberRow("Blended hourly rate", key: TargetsPayload.Field.hourly, prefix: "$",
                      note: p.noteFor(TargetsPayload.Field.hourly) ?? "Costs an hour nobody has a rate for.")
            if p.roles.isEmpty {
                Text("No roles yet \u{2014} they come from your shifts and the roster.")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3).padding(.vertical, 9)
            }
            ForEach(p.roles, id: \.self) { role in
                roleRow(p, role: role)
            }
            if !p.salariedRoles.isEmpty {
                Text("\(p.salariedRoles.joined(separator: ", ")) \(p.salariedRoles.count == 1 ? "isn\u{2019}t" : "aren\u{2019}t") listed: everyone in \(p.salariedRoles.count == 1 ? "it" : "them") is salaried, set below.")
                    .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 6)
            }
        }
    }

    @ViewBuilder
    private func roleRow(_ p: TargetsPayload, role: String) -> some View {
        let people = p.peopleByRole[role] ?? []
        let pay = p.rolePay[role]
        let open = openRoles.contains(role)
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(role).font(.cavnarBody(15.5, weight: 700)).foregroundStyle(Color.cavnarInk)
                    if !people.isEmpty {
                        Button {
                            Haptic.selection()
                            withAnimation(.easeOut(duration: 0.2)) {
                                if open { openRoles.remove(role) } else { openRoles.insert(role) }
                            }
                        } label: {
                            HStack(spacing: 4) {
                                HomeMixedText.make(TargetsPayload.peopleLine(people.count, unrated: pay?.unrated ?? 0),
                                                   size: 13.5, weight: 600, color: .cavnarEmber2)
                                Image(systemName: open ? "chevron.up" : "chevron.down").font(.system(size: 10, weight: .bold))
                                    .foregroundStyle(Color.cavnarEmber2)
                            }
                            .frame(minHeight: 30)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                Spacer(minLength: 8)
                if let range = pay?.rangeText {
                    // What the POS pays this role's people — shown as it is.
                    VStack(alignment: .trailing, spacing: 0) {
                        Text(range).font(.cavnarNumber(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Text("an hour").font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    }
                } else {
                    rateField(key: TargetsPayload.Field.role(role), placeholder: p.hourlyRate.map(TargetsPayload.money) ?? "")
                }
            }
            .padding(.vertical, 9)
            if open {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(people) { person in
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(person.name).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk2)
                                if person.posRate == nil && person.rate == nil, let fb = pay?.fallback {
                                    HomeMixedText.make("No rate on your POS \u{00B7} costed at \(TargetsPayload.money(fb)) until you set one",
                                                       size: 12.5, weight: 500, color: .cavnarAmber)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            Spacer(minLength: 8)
                            if let pos = person.posRate {
                                VStack(alignment: .trailing, spacing: 0) {
                                    Text(TargetsPayload.money(pos)).font(.cavnarNumber(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                                    Text("from your POS").font(.cavnarBody(11.5)).foregroundStyle(Color.cavnarInk3)
                                }
                            } else {
                                rateField(key: TargetsPayload.Field.person(person.name),
                                          placeholder: pay?.fallback.map(TargetsPayload.money) ?? "")
                            }
                        }
                        .padding(.leading, 12)
                        .padding(.vertical, 7)
                    }
                }
                .transition(.opacity)
            }
            AccountRowDivider()
        }
    }

    // MARK: Salaried

    private func salariedSection(_ p: TargetsPayload) -> some View {
        AccountSection(kicker: p.salaried.isEmpty ? "Salaried staff" : "Salaried staff \u{00B7} \(p.salaried.count)") {
            if p.salaried.isEmpty {
                Text("Nobody yet. Add the people paid a salary and Labor shows their pay beside hourly labor.")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 9)
            }
            ForEach(p.salaried) { entry in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .top, spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(entry.name).font(.cavnarBody(15.5, weight: 700)).foregroundStyle(Color.cavnarInk)
                            HomeMixedText.make(TargetsPayload.salaryLine(entry, perDay: p.perDay[entry.name]),
                                               size: 13.5, weight: 500, color: .cavnarInk3)
                        }
                        Spacer(minLength: 8)
                        if model.busy == entry.name {
                            CavnarShimmerLine(color: .cavnarRed).frame(width: 28)
                        } else {
                            AccountActionChip(symbol: "xmark", tone: .cavnarRed, accessibilityLabel: "Remove \(entry.name)") {
                                pendingSalariedRemove = entry
                            }
                        }
                    }
                    if let warning = entry.warning {
                        Text(warning).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let suggestion = entry.suggestion {
                        Button {
                            Task { await model.linkSalaried(entry, to: suggestion) }
                        } label: {
                            Text("Link to \(suggestion)")
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .disabled(model.busy != nil)
                    }
                }
                .padding(.vertical, 9)
                .contextMenu {
                    Button(role: .destructive) { pendingSalariedRemove = entry } label: {
                        Label("Remove", systemImage: "trash")
                    }
                }
                AccountRowDivider()
            }
            // Add one person — one save, never the whole list.
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    TextField("Name, as on your POS", text: $newSalariedName)
                        .font(.cavnarBody(15.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .textInputAutocapitalization(.words)
                        .focused($focus, equals: "salaried-name")
                    if !p.salariedNames.isEmpty {
                        Menu {
                            ForEach(p.salariedNames, id: \.self) { n in
                                Button(n) { newSalariedName = n }
                            }
                        } label: {
                            Image(systemName: "person.crop.circle.badge.plus")
                                .foregroundStyle(Color.cavnarEmber)
                                .frame(width: 36, height: 36)
                        }
                        .accessibilityLabel("Pick from the roster")
                    }
                }
                HStack(spacing: 10) {
                    Text("$").font(.cavnarNumber(15.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
                    TextField("Annual salary", text: $newSalariedAnnual)
                        .font(.cavnarNumber(15.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .keyboardType(.numberPad)
                        .focused($focus, equals: "salaried-annual")
                    Spacer(minLength: 8)
                    Button {
                        focus = nil
                        Task {
                            if await model.addSalaried(name: newSalariedName, annual: newSalariedAnnual) {
                                newSalariedName = ""
                                newSalariedAnnual = ""
                            }
                        }
                    } label: {
                        Text("Add")
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: model.busy != nil))
                    .disabled(model.busy != nil)
                }
            }
            .padding(.vertical, 9)
        }
    }

    // MARK: Fields

    private func numberRow(_ label: String, key: String, prefix: String? = nil, suffix: String? = nil,
                           placeholder: String = "", note: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 10) {
                Text(label).font(.cavnarBody(15.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 8)
                if let prefix { Text(prefix).font(.cavnarNumber(15.5, weight: 600)).foregroundStyle(Color.cavnarInk3) }
                TextField(placeholder.isEmpty ? "\u{2014}" : placeholder, text: model.binding(key))
                    .font(.cavnarNumber(16, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .multilineTextAlignment(.trailing)
                    .keyboardType(.decimalPad)
                    .frame(width: 110)
                    .focused($focus, equals: key)
                    .disabled(!model.canEdit)
                    .accessibilityLabel(label)
                if let suffix { Text(suffix).font(.cavnarNumber(15.5, weight: 600)).foregroundStyle(Color.cavnarInk3) }
                if model.busy == key { CavnarShimmerLine().frame(width: 22) }
            }
            if let note {
                HomeMixedText.make(note, size: 12.5, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            AccountRowDivider()
        }
        .padding(.top, 9)
    }

    private func rateField(key: String, placeholder: String) -> some View {
        HStack(spacing: 4) {
            Text("$").font(.cavnarNumber(14.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
            TextField(placeholder.replacingOccurrences(of: "$", with: ""), text: model.binding(key))
                .font(.cavnarNumber(15, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .multilineTextAlignment(.trailing)
                .keyboardType(.decimalPad)
                .frame(width: 72)
                .focused($focus, equals: key)
                .disabled(!model.canEdit)
            if model.busy == key { CavnarShimmerLine().frame(width: 18) }
        }
    }
}

// MARK: - Payload

/// GET /account/targets → `targets`, read leniently: a target that isn't
/// set is nil (shown "—"), never 0.
struct TargetsPayload: Decodable {
    var laborTargetPct: Double?
    var foodCostTarget: Double?
    var wasteTargetPct: Double?
    var monthlyRevenueTarget: Double?
    var weeklyRevenueTarget: Double?
    var hourlyRate: Double?
    var weekStartDay = 0
    var roleRates: [String: Double] = [:]
    var roles: [String] = []
    var salariedRoles: [String] = []
    var peopleByRole: [String: [Person]] = [:]
    var rolePay: [String: RolePay] = [:]
    var salaried: [SalariedEntry] = []
    var perDay: [String: Double] = [:]
    var salariedNames: [String] = []
    var setNotes: [String: String] = [:]
    var laborGoal: String?
    var foodGoal: String?

    static let weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    /// A month is 52 ÷ 12 weeks (models.weekly_revenue_target).
    static let weeksPerMonth = 52.0 / 12.0

    /// The keys a field saves under (the server's own names; a role's rate
    /// and a person's are keyed so the model knows which body to send).
    enum Field {
        static let labor = "labor_target_pct"
        static let food = "food_cost_target"
        static let waste = "waste_target_pct"
        static let monthly = "monthly_revenue_target"
        static let weekly = "weekly_revenue_target"
        static let hourly = "hourly_rate"
        static func role(_ r: String) -> String { "role:" + r }
        static func person(_ n: String) -> String { "person:" + n }
    }

    struct Person: Decodable, Identifiable, Hashable {
        let name: String
        let posRate: Double?
        let rate: Double?
        var id: String { name }
        enum CodingKeys: String, CodingKey { case name, rate; case posRate = "pos_rate" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = c.setupText(.name) ?? ""
            posRate = c.setupDouble(.posRate)
            rate = c.setupDouble(.rate)
        }
    }

    struct RolePay: Decodable {
        let low: Double?
        let high: Double?
        let people: Int?
        let unrated: Int?
        let fallback: Double?
        enum CodingKeys: String, CodingKey { case low, high, people, unrated, fallback }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            low = c.setupDouble(.low); high = c.setupDouble(.high)
            people = c.setupInt(.people); unrated = c.setupInt(.unrated)
            fallback = c.setupDouble(.fallback)
        }
        /// "$15–$22" — what the role's people are paid, when anyone is.
        var rangeText: String? {
            guard let low else { return nil }
            if let high, high != low { return TargetsPayload.money(low) + "\u{2013}" + TargetsPayload.money(high) }
            return TargetsPayload.money(low)
        }
    }

    private struct Goal: Decodable { let label: String? }
    private struct GoalBox: Decodable { let goal: Goal? }

    enum CodingKeys: String, CodingKey {
        case roles, salaried, labor, food
        case laborTargetPct = "labor_target_pct"
        case foodCostTarget = "food_cost_target"
        case wasteTargetPct = "waste_target_pct"
        case monthlyRevenueTarget = "monthly_revenue_target"
        case weeklyRevenueTarget = "weekly_revenue_target"
        case hourlyRate = "hourly_rate"
        case weekStartDay = "week_start_day"
        case roleRates = "role_rates"
        case salariedRoles = "salaried_roles"
        case peopleByRole = "people_by_role"
        case rolePay = "role_pay"
        case salariedNames = "salaried_names"
        case setNotes = "set_notes"
    }

    private struct PerDay: Decodable {
        let name: String?
        let perDay: Double?
        enum CodingKeys: String, CodingKey { case name; case perDay = "per_day" }
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        laborTargetPct = c.setupDouble(.laborTargetPct)
        foodCostTarget = c.setupDouble(.foodCostTarget)
        wasteTargetPct = c.setupDouble(.wasteTargetPct)
        monthlyRevenueTarget = c.setupDouble(.monthlyRevenueTarget)
        weeklyRevenueTarget = c.setupDouble(.weeklyRevenueTarget)
        hourlyRate = c.setupDouble(.hourlyRate)
        weekStartDay = min(max(c.setupInt(.weekStartDay) ?? 0, 0), 6)
        roleRates = ((try? c.decodeIfPresent([String: Double].self, forKey: .roleRates)) ?? nil) ?? [:]
        roles = c.setupTexts(.roles)
        salariedRoles = c.setupTexts(.salariedRoles)
        peopleByRole = ((try? c.decodeIfPresent([String: HomeLenientListDecodable<Person>].self, forKey: .peopleByRole)) ?? nil)?
            .mapValues { $0.items.filter { !$0.name.isEmpty } } ?? [:]
        rolePay = ((try? c.decodeIfPresent([String: RolePay].self, forKey: .rolePay)) ?? nil) ?? [:]
        salaried = c.setupList(SalariedEntry.self, .salaried).filter { !$0.name.isEmpty }
        for row in c.setupList(PerDay.self, .salaried) {
            if let n = row.name, let d = row.perDay { perDay[n] = d }
        }
        salariedNames = c.setupTexts(.salariedNames)
        setNotes = ((try? c.decodeIfPresent([String: String].self, forKey: .setNotes)) ?? nil) ?? [:]
        laborGoal = ((try? c.decodeIfPresent(GoalBox.self, forKey: .labor)) ?? nil)?.goal?.label
        foodGoal = ((try? c.decodeIfPresent(GoalBox.self, forKey: .food)) ?? nil)?.goal?.label
    }

    /// What a field reads as typed: "30", "28.5", "" when it isn't set.
    func text(for key: String) -> String {
        func n(_ v: Double?) -> String {
            guard let v else { return "" }
            return v == v.rounded() ? String(Int(v)) : String(format: "%.2f", v)
        }
        switch key {
        case Field.labor: return n(laborTargetPct)
        case Field.food: return n(foodCostTarget)
        case Field.waste: return n(wasteTargetPct)
        case Field.monthly: return (monthlyRevenueTarget ?? 0) > 0 ? String(Int(monthlyRevenueTarget!.rounded())) : ""
        case Field.weekly: return (weeklyRevenueTarget ?? 0) > 0 ? n((weeklyRevenueTarget! * 100).rounded() / 100) : ""
        case Field.hourly: return n(hourlyRate)
        default:
            if key.hasPrefix("role:") {
                let role = String(key.dropFirst(5)).lowercased()
                return n(roleRates.first { $0.key.lowercased() == role }?.value)
            }
            if key.hasPrefix("person:") {
                let name = String(key.dropFirst(7))
                return n(peopleByRole.values.flatMap { $0 }.first { $0.name == name }?.rate)
            }
            return ""
        }
    }

    /// "Your goal of 26% by 12/31/26 applies · Set by the owner on 9/12/26".
    func noteFor(_ key: String) -> String? {
        var parts: [String] = []
        if key == Field.labor, let g = laborGoal { parts.append(g + " applies") }
        if key == Field.food, let g = foodGoal { parts.append(g + " applies") }
        if let set = setNotes[key] { parts.append(set) }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }

    static func money(_ v: Double) -> String {
        v == v.rounded() ? "$\(Int(v))" : String(format: "$%.2f", v)
    }

    static func peopleLine(_ count: Int, unrated: Int) -> String {
        "\(count) \(count == 1 ? "person" : "people")" + (unrated > 0 ? " \u{00B7} \(unrated) without a rate" : "")
    }

    static func salaryLine(_ e: SalariedEntry, perDay: Double?) -> String {
        let fmt = NumberFormatter()
        fmt.numberStyle = .decimal
        fmt.maximumFractionDigits = 0
        let annual = e.annual.flatMap { fmt.string(from: NSNumber(value: $0)) }.map { "$" + $0 + " a year" } ?? "\u{2014}"
        let day = perDay.flatMap { fmt.string(from: NSNumber(value: $0)) }.map { " \u{00B7} $" + $0 + " a trading day" } ?? ""
        return annual + day
    }
}

/// POST /account/targets — one change per save. Every key is omitted unless
/// set; `clearWaste` sends waste_target_pct as null, and a person's rate of
/// nil sends null (back to the role's rate), as the web does.
struct TargetsBody: Encodable, Equatable {
    var number: [String: Double] = [:]
    var clearWaste = false
    var weekStartDay: Int? = nil
    var roleRates: [String: Double]? = nil
    var personRate: PersonRate? = nil
    var salariedAdd: SalariedAdd? = nil
    var salariedRemove: String? = nil

    struct PersonRate: Encodable, Equatable {
        let name: String
        let rate: Double?
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: Key.self)
            try c.encode(name, forKey: Key("name"))
            if let rate { try c.encode(rate, forKey: Key("rate")) } else { try c.encodeNil(forKey: Key("rate")) }
        }
    }

    struct SalariedAdd: Encodable, Equatable {
        let name: String
        let annual: Double
    }

    struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(_ s: String) { stringValue = s }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Key.self)
        for (k, v) in number { try c.encode(v, forKey: Key(k)) }
        if clearWaste { try c.encodeNil(forKey: Key("waste_target_pct")) }
        if let weekStartDay { try c.encode(weekStartDay, forKey: Key("week_start_day")) }
        if let roleRates { try c.encode(roleRates, forKey: Key("role_rates")) }
        if let personRate { try c.encode(personRate, forKey: Key("person_rate")) }
        if let salariedAdd { try c.encode(salariedAdd, forKey: Key("salaried_add")) }
        if let salariedRemove { try c.encode(salariedRemove, forKey: Key("salaried_remove")) }
    }

    /// The body that saves `key` as typed — nil when there's nothing to send
    /// (an unchanged or unreadable value). Pure, so a test pins it.
    static func forField(_ key: String, typed: String, current: TargetsPayload) -> TargetsBody? {
        let t = typed.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: "")
            .replacingOccurrences(of: "$", with: "").replacingOccurrences(of: "%", with: "")
        let value = Double(t)
        if t == current.text(for: key) { return nil }
        if key.hasPrefix("person:") {
            if !t.isEmpty && value == nil { return nil }
            return TargetsBody(personRate: PersonRate(name: String(key.dropFirst(7)), rate: value))
        }
        if key.hasPrefix("role:") {
            let role = String(key.dropFirst(5))
            if !t.isEmpty && value == nil { return nil }
            // Only this role changes; every other role keeps its rate.
            var rates = current.roleRates.filter { $0.key.lowercased() != role.lowercased() }
            if let value { rates[role] = value }
            return TargetsBody(roleRates: rates)
        }
        if t.isEmpty {
            if key == TargetsPayload.Field.waste { return TargetsBody(clearWaste: true) }
            if key == TargetsPayload.Field.monthly || key == TargetsPayload.Field.weekly {
                return TargetsBody(number: [key: 0])
            }
            return nil
        }
        guard let value else { return nil }
        return TargetsBody(number: [key: value])
    }
}

// MARK: - Model

@Observable
@MainActor
final class AccountTargetsModel {
    var payload: TargetsPayload?
    var canEdit = false
    var seesPay = false
    var isLoading = false
    var error: String?
    var posted: String?
    /// The field (or salaried name) being saved.
    var busy: String?
    /// What each field reads as typed, keyed like TargetsPayload.Field.
    var drafts: [String: String] = [:]

    private struct Response: Decodable {
        let ok: Bool
        let error: String?
        let targets: TargetsPayload?
        let canEdit: Bool?
        let seesPay: Bool?
        enum CodingKeys: String, CodingKey { case ok, error, targets; case canEdit = "can_edit"; case seesPay = "sees_pay" }
    }

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func binding(_ key: String) -> Binding<String> {
        Binding(get: { [weak self] in self?.drafts[key] ?? self?.payload?.text(for: key) ?? "" },
                set: { [weak self] v in self?.typed(key, v) })
    }

    /// Typing in one revenue box fills the other (by 52 ÷ 12); only the box
    /// typed in is saved.
    private func typed(_ key: String, _ value: String) {
        drafts[key] = value
        let v = Double(value.replacingOccurrences(of: ",", with: ""))
        if key == TargetsPayload.Field.monthly {
            drafts[TargetsPayload.Field.weekly] = v.map { $0 > 0 ? String(format: "%.0f", $0 / TargetsPayload.weeksPerMonth) : "" } ?? ""
        } else if key == TargetsPayload.Field.weekly {
            drafts[TargetsPayload.Field.monthly] = v.map { $0 > 0 ? String(format: "%.0f", $0 * TargetsPayload.weeksPerMonth) : "" } ?? ""
        }
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let r: Response = try await client.send("/mobile/api/account/targets", hapticOnError: false)
            guard r.ok, let t = r.targets else { error = r.error ?? "Couldn\u{2019}t load your targets."; return }
            payload = t
            canEdit = r.canEdit ?? false
            seesPay = r.seesPay ?? false
            drafts = [:]
            error = nil
        } catch let e as APIClient.APIError {
            error = e.message
        } catch is CancellationError {
        } catch {
            if payload == nil { self.error = "Couldn\u{2019}t load your targets." }
        }
    }

    /// Saves the field the owner just left, if it changed. What was typed
    /// stays in the box until the server took it (re-audit 10/8/26, #4: a
    /// refused or failed save cleared the draft, and the figure vanished
    /// back to the old one with only the error to say why).
    func commit(_ key: String) async {
        guard canEdit, let p = payload, let typed = drafts[key],
              let body = TargetsBody.forField(key, typed: typed, current: p) else { return }
        guard await save(body, busyKey: key, done: "Saved") else { return }
        drafts[key] = nil
        // The revenue pair: the box not typed in follows the server's answer.
        if key == TargetsPayload.Field.monthly { drafts[TargetsPayload.Field.weekly] = nil }
        if key == TargetsPayload.Field.weekly { drafts[TargetsPayload.Field.monthly] = nil }
    }

    func setWeekStart(_ day: Int) async {
        guard canEdit, day != payload?.weekStartDay else { return }
        await save(TargetsBody(weekStartDay: day), busyKey: "week", done: "Payroll week saved")
    }

    @discardableResult
    func addSalaried(name: String, annual: String) async -> Bool {
        let n = name.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { error = "Add a name."; return false }
        guard let a = Double(annual.replacingOccurrences(of: ",", with: "").replacingOccurrences(of: "$", with: "")), a > 0 else {
            error = "Add the annual salary."
            return false
        }
        return await save(TargetsBody(salariedAdd: .init(name: n, annual: a)), busyKey: n, done: "\(n) added")
    }

    func removeSalaried(_ name: String) async {
        await save(TargetsBody(salariedRemove: name), busyKey: name, done: "\(name) removed")
    }

    /// Saved again under the roster's spelling — one add and one remove in
    /// a single save, so the salary is never counted twice or dropped.
    func linkSalaried(_ entry: SalariedEntry, to name: String) async {
        guard let annual = entry.annual, annual > 0 else { return }
        await save(TargetsBody(salariedAdd: .init(name: name, annual: annual), salariedRemove: entry.name),
                   busyKey: entry.name, done: "Linked to \(name)")
    }

    @discardableResult
    private func save(_ body: TargetsBody, busyKey: String, done: String) async -> Bool {
        busy = busyKey
        error = nil
        defer { busy = nil }
        do {
            let r: Response = try await client.send("/mobile/api/account/targets", method: .post, body: body,
                                                    hapticOnError: false, retryTransient: false)
            guard r.ok, let t = r.targets else {
                error = r.error ?? "Couldn\u{2019}t save that."
                Haptic.error()
                return false
            }
            payload = t
            Haptic.success()
            posted = done
            return true
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t save that."
        }
        Haptic.error()
        return false
    }
}
