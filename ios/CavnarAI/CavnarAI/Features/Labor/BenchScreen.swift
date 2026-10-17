import SwiftUI
import Observation

// The Studio's Bench on the phone (schedule audit 10/10/26, J3 —
// `/mobile/api/labor/bench`, studio_bench.api_body): where one absence
// leaves a gap and who to train next (MARKET-32), the nights likely to lose
// someone with a person off who could be on call, priced against what a
// short night costs (MARKET-26), events that changed after the week went
// out (MARKET-28), and — the owner's alone — the people the schedule may be
// losing (MARKET-29; nobody is told). Each part loads on its own; a part
// this login may not see (403) is left out.

struct BenchStrength: Decodable {
    struct Pool: Decodable, Identifiable, Hashable {
        var family: String; var daypart: String; var status: String; var why: String?
        var id: String { "\(family)|\(daypart)" }
        enum CodingKeys: String, CodingKey { case family, daypart, status, why }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            family = c.sfText(.family) ?? ""; daypart = c.sfText(.daypart) ?? ""
            status = c.sfText(.status) ?? "ok"; why = c.sfText(.why)
        }
    }
    struct Line: Decodable, Hashable { var text: String? }
    var available = false
    var pools: [Pool] = []
    var crossTrain: [Line] = []
    var hiring: [Line] = []
    var basis: String?
    enum CodingKeys: String, CodingKey { case available, pools, hiring, basis
        case crossTrain = "cross_train" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.sfBool(.available) ?? false
        pools = c.setupList(Pool.self, .pools).filter { $0.status != "ok" }
        crossTrain = c.setupList(Line.self, .crossTrain)
        hiring = c.setupList(Line.self, .hiring)
        basis = c.sfText(.basis)
    }
}

struct BenchStandby: Decodable {
    struct Person: Decodable, Hashable {
        var employee: String?; var role: String?
        var shiftStart: String?; var shiftEnd: String?
        enum CodingKeys: String, CodingKey { case employee, role
            case shiftStart = "shift_start"; case shiftEnd = "shift_end" }
    }
    struct Price: Decodable, Hashable {
        var standbyPay: Double?; var expectedShortCost: Double?; var verdict: String?
        enum CodingKeys: String, CodingKey { case verdict
            case standbyPay = "standby_pay"; case expectedShortCost = "expected_short_cost" }
    }
    struct Suggestion: Decodable, Identifiable, Hashable {
        var date: String; var day: String?; var chance: Double?
        var standby: Person?; var price: Price?; var taken: Bool
        var id: String { date }
        enum CodingKeys: String, CodingKey { case date, day, standby, price, slots
            case chance = "chance_of_a_no_show" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let d = c.sfText(.date) else {
                throw DecodingError.dataCorruptedError(forKey: .date, in: c, debugDescription: "no date")
            }
            date = d; day = c.sfText(.day); chance = c.sfDouble(.chance)
            standby = (try? c.decodeIfPresent(Person.self, forKey: .standby)) ?? nil
            price = (try? c.decodeIfPresent(Price.self, forKey: .price)) ?? nil
            taken = !(((try? c.decodeIfPresent([JSONValue].self, forKey: .slots)) ?? nil) ?? []).isEmpty
        }
    }
    struct Slot: Decodable, Identifiable, Hashable {
        var id: Int; var status: String; var employee: String; var date: String
        var shiftStart: String?; var shiftEnd: String?; var calledIn: Bool; var payOwed: Double?
        enum CodingKeys: String, CodingKey { case id, status, date
            case employee = "employee_name"; case shiftStart = "shift_start"; case shiftEnd = "shift_end"
            case calledIn = "called_in"; case payOwed = "pay_owed" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let i = c.sfInt(.id) else {
                throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "no id")
            }
            id = i; status = c.sfText(.status) ?? ""; employee = c.sfText(.employee) ?? ""
            date = c.sfText(.date) ?? ""; shiftStart = c.sfText(.shiftStart); shiftEnd = c.sfText(.shiftEnd)
            calledIn = c.sfBool(.calledIn) ?? false; payOwed = c.sfDouble(.payOwed)
        }
    }
    struct Policy: Decodable, Hashable {
        var payHours: Double?; var flatPay: Double?; var stated: Bool?
        enum CodingKeys: String, CodingKey { case stated
            case payHours = "pay_hours"; case flatPay = "flat_pay" }
    }
    var suggestions: [Suggestion] = []
    var slots: [Slot] = []
    var historyId: Int?
    var policy: Policy?
    enum CodingKeys: String, CodingKey { case suggestions, slots, policy
        case historyId = "history_id" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        suggestions = c.setupList(Suggestion.self, .suggestions)
        slots = c.setupList(Slot.self, .slots)
        historyId = c.sfInt(.historyId)
        policy = (try? c.decodeIfPresent(Policy.self, forKey: .policy)) ?? nil
    }

    /// "2 hours at their rate (the default)" / "$40 a night".
    var policyLine: String {
        guard let p = policy else { return "" }
        let what = p.flatPay.map { "$\($0.commaFormatted) a night" }
            ?? "\(String(format: "%g", p.payHours ?? 2)) hours at their rate"
        return "Standby pay: " + what + (p.stated == true ? "" : " (the default)")
    }
}

/// The owner's other locations and whether the bench is on here (org_bench).
struct BenchOrg: Decodable {
    var enabled = false
    var siblings = 0
    var members = 0
    enum CodingKeys: String, CodingKey { case enabled, siblings, members }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = c.sfBool(.enabled) ?? false
        siblings = ((try? c.decodeIfPresent([JSONValue].self, forKey: .siblings)) ?? nil)?.count ?? 0
        members = ((try? c.decodeIfPresent([JSONValue].self, forKey: .members)) ?? nil)?.count ?? 0
    }
}

struct BenchRetention: Decodable {
    struct Person: Decodable, Identifiable, Hashable {
        var name: String; var level: String; var risk: Double?; var reasons: [String]; var protect: Bool
        var valuedBecause: String?
        var id: String { name }
        enum CodingKeys: String, CodingKey { case name, level, risk, reasons, protect
            case valuedBecause = "valued_because" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = c.sfText(.name) ?? ""; level = c.sfText(.level) ?? "none"; risk = c.sfDouble(.risk)
            reasons = c.setupTexts(.reasons); protect = c.sfBool(.protect) ?? false
            valuedBecause = c.sfText(.valuedBecause)
        }
    }
    var people: [Person] = []
    var basis: String?
    enum CodingKeys: String, CodingKey { case people, basis }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        people = c.setupList(Person.self, .people).filter { $0.level != "none" }
        basis = c.sfText(.basis)
    }
}

struct BenchEvents: Decodable {
    struct Proposal: Decodable, Identifiable, Hashable {
        var id: Int; var date: String; var summary: String; var status: String; var priceText: String?
        enum CodingKeys: String, CodingKey { case id, date, summary, status, cost }
        enum CostKeys: String, CodingKey { case predictabilityText = "predictability_text" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let i = c.sfInt(.id) else {
                throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "no id")
            }
            id = i; date = c.sfText(.date) ?? ""; summary = c.sfText(.summary) ?? ""; status = c.sfText(.status) ?? ""
            priceText = (try? c.nestedContainer(keyedBy: CostKeys.self, forKey: .cost))?.sfText(.predictabilityText)
        }
    }
    var proposals: [Proposal] = []
    enum CodingKeys: String, CodingKey { case proposals }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        proposals = c.setupList(Proposal.self, .proposals)
    }
}

/// POST labor/bench {action, …}: the standby offer and withdrawal, event
/// answers, and the owner's protect switch.
struct BenchActionBody: Encodable {
    let action: String
    var date: String? = nil
    var role: String? = nil
    var employee: String? = nil
    var shiftStart: String? = nil
    var shiftEnd: String? = nil
    var historyId: Int? = nil
    var chance: Double? = nil
    var expectedCost: Double? = nil
    var slotId: Int? = nil
    var proposalId: Int? = nil
    var name: String? = nil
    var protect: Bool? = nil
    /// The standby policy: hours of pay at the person's rate.
    var payHours: Double? = nil
    /// The bench at this location, on or off (owner only).
    var enabled: Bool? = nil
    enum CodingKeys: String, CodingKey {
        case action, date, role, employee, chance, name, protect, enabled
        case payHours = "pay_hours"
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case historyId = "history_id"
        case expectedCost = "expected_cost"
        case slotId = "slot_id"
        case proposalId = "proposal_id"
    }
}

@Observable
@MainActor
final class BenchStore {
    var strength: BenchStrength?
    var standby: BenchStandby?
    var retention: BenchRetention?
    var events: BenchEvents?
    var org: BenchOrg?
    var busy: String?
    var error: String?
    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load(historyId: Int?) async {
        var q = ["part": "strength"]
        strength = try? await client.send("/mobile/api/labor/bench", query: q, hapticOnError: false)
        q["part"] = "standby"
        if let historyId { q["history_id"] = String(historyId) }
        standby = try? await client.send("/mobile/api/labor/bench", query: q, hapticOnError: false)
        // The owner's alone: a 403 for anyone else, and the part is left out.
        retention = try? await client.send("/mobile/api/labor/bench", query: ["part": "retention"], hapticOnError: false)
        events = try? await client.send("/mobile/api/labor/bench", query: ["part": "events"], hapticOnError: false)
        org = try? await client.send("/mobile/api/labor/bench", query: ["part": "org"], hapticOnError: false)
    }

    func post(_ body: BenchActionBody, key: String, historyId: Int?) async {
        busy = key
        error = nil
        defer { busy = nil }
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/labor/bench", method: .post, body: body,
                                                                hapticOnError: false, retryTransient: false)
            if r.ok { Haptic.success() } else { error = r.error ?? "That didn\u{2019}t go through." }
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "That didn\u{2019}t go through."
        }
        await load(historyId: historyId)
    }
}

struct BenchScreen: View {
    let historyId: Int?
    @State private var store = BenchStore()
    @State private var payHours: Double = 2

    private static func money(_ v: Double?) -> String { v.map { "$" + $0.commaFormatted } ?? "\u{2014}" }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    if let e = store.error { CavnarMixedText(e, role: .secondary, color: .cavnarRedText) }
                    strengthCard
                    standbyCard
                    eventsCard
                    retentionCard
                    orgCard
                    CavnarWebLinkRow(title: "What a cut costs the guest, tips and your other locations",
                                     path: "labor/schedule", actionLabel: "Open on the web")
                }
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Bench")
        }
        .task {
            await store.load(historyId: historyId)
            payHours = store.standby?.policy?.payHours ?? 2
        }
    }

    @ViewBuilder
    private var strengthCard: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Bench strength")
            if let s = store.strength {
                if !s.available {
                    Text("Fills in once a few weeks of shifts are on file.").cavnarText(.secondary)
                } else if s.pools.isEmpty {
                    CavnarMixedText("No role and shift is one absence from a gap.", role: .lead, color: .cavnarInk)
                }
                ForEach(s.pools) { p in
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        TonePill(text: p.status.capitalized, tone: p.status == "critical" ? .bad : .warning)
                        CavnarMixedText("\(p.family) \(p.daypart == "morning" ? "lunch" : "dinner")" + (p.why.map { ": \($0)" } ?? ""),
                                        role: .body, color: .cavnarInk)
                    }
                }
                ForEach(Array(s.crossTrain.prefix(3).enumerated()), id: \.offset) { _, l in
                    if let t = l.text { CavnarMixedText(t, role: .secondary) }
                }
                ForEach(Array(s.hiring.enumerated()), id: \.offset) { _, l in
                    if let t = l.text { CavnarMixedText(t, role: .body, color: .cavnarInk) }
                }
                if let b = s.basis { Text(b).cavnarText(.caption) }
            } else {
                CavnarSkeletonLines(widths: [1.0, 0.7])
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    @ViewBuilder
    private var standbyCard: some View {
        if let s = store.standby, !(s.suggestions.isEmpty && s.slots.isEmpty) {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                CavnarKicker("On call for no-shows")
                ForEach(s.suggestions) { sug in
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        CavnarMixedText(CavnarDate.dayDate(sug.day, sug.date)
                                        + (sug.chance.map { ": about \(Int(($0 * 100).rounded()))% chance someone doesn\u{2019}t show" } ?? ""),
                                        role: .label)
                        if let who = sug.standby, let name = who.employee {
                            CavnarMixedText("\(name) is off and could be on call \u{00B7} \(Self.money(sug.price?.standbyPay)) standby pay"
                                            + (sug.price?.expectedShortCost.map { " \u{00B7} a short night costs about \(Self.money($0))" } ?? ""),
                                            role: .secondary)
                            if let v = sug.price?.verdict { Text(v).cavnarText(.caption) }
                            if !sug.taken {
                                Button {
                                    Task {
                                        await store.post(BenchActionBody(action: "standby_offer", date: sug.date, role: who.role,
                                                                         employee: name, shiftStart: who.shiftStart,
                                                                         shiftEnd: who.shiftEnd, historyId: s.historyId ?? historyId,
                                                                         chance: sug.chance,
                                                                         expectedCost: sug.price?.expectedShortCost),
                                                         key: "sb\(sug.date)", historyId: historyId)
                                    }
                                } label: { Text("Offer \(name) on call").frame(maxWidth: .infinity) }
                                    .buttonStyle(CavnarSecondaryButtonStyle())
                                    .disabled(store.busy != nil)
                            }
                        }
                    }
                }
                ForEach(s.slots) { slot in
                    HStack(alignment: .firstTextBaseline) {
                        CavnarMixedText("\(slot.employee) \u{00B7} \(CavnarDate.mdy(slot.date)) \u{00B7} "
                                        + (slot.calledIn ? "called in" : slot.status)
                                        + (slot.payOwed.map { " \u{00B7} \(Self.money($0)) owed" } ?? ""), role: .secondary)
                        Spacer(minLength: CavnarSpace.xs)
                        if slot.status == "offered" || slot.status == "accepted" {
                            Button {
                                Task { await store.post(BenchActionBody(action: "standby_withdraw", slotId: slot.id),
                                                        key: "w\(slot.id)", historyId: historyId) }
                            } label: { Text("Withdraw").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget() }
                                .buttonStyle(.plain)
                                .disabled(store.busy != nil)
                        }
                    }
                }
                if !s.policyLine.isEmpty {
                    HStack {
                        Text(s.policyLine).cavnarText(.secondary)
                        Spacer(minLength: CavnarSpace.xs)
                        Stepper("Hours of standby pay", value: $payHours, in: 0...12, step: 0.5)
                            .labelsHidden()
                    }
                    if payHours != (s.policy?.payHours ?? 2) {
                        Button {
                            Task { await store.post(BenchActionBody(action: "standby_policy", payHours: payHours),
                                                    key: "policy", historyId: historyId) }
                        } label: { Text("Pay \(String(format: "%g", payHours)) hours a night on call").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                            .disabled(store.busy != nil)
                    }
                }
                CavnarEvidenceSection(label: "See how it works") {
                    Text("A yes is kept with the time and the exact terms. If someone doesn\u{2019}t show, the person on call is offered the shift at once.")
                        .cavnarText(.caption)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    @ViewBuilder
    private var eventsCard: some View {
        let open = (store.events?.proposals ?? []).filter { $0.status == "open" }
        if !open.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                CavnarKicker("Events that changed after you sent the week", tint: .cavnarAmber)
                ForEach(open) { p in
                    VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                        CavnarMixedText("\(CavnarDate.mdy(p.date)) \u{00B7} \(p.summary)", role: .body, color: .cavnarInk)
                        if let t = p.priceText { Text(t).cavnarText(.caption) }
                        HStack(spacing: CavnarSpace.s) {
                            Button {
                                Task { await store.post(BenchActionBody(action: "event_approve", proposalId: p.id),
                                                        key: "e\(p.id)", historyId: historyId) }
                            } label: { Text("Make the change").frame(maxWidth: .infinity) }
                                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: store.busy != nil))
                                .disabled(store.busy != nil)
                            Button {
                                Task { await store.post(BenchActionBody(action: "event_dismiss", proposalId: p.id),
                                                        key: "d\(p.id)", historyId: historyId) }
                            } label: { Text("Keep the week").frame(maxWidth: .infinity) }
                                .buttonStyle(CavnarSecondaryButtonStyle())
                                .disabled(store.busy != nil)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    @ViewBuilder
    private var orgCard: some View {
        if let o = store.org, o.siblings > 0 {
            AccountSection(kicker: "Your other locations") {
                AccountSwitchRow(label: "Share the bench here",
                                 detail: "\(o.members) of your staff opted in to work at your other locations. On: their certifications and attendance count when this location ranks them for a gap.",
                                 isOn: Binding(get: { o.enabled }, set: { on in
                                     Task { await store.post(BenchActionBody(action: "bench_site", enabled: on),
                                                             key: "org", historyId: historyId) }
                                 }),
                                 busy: store.busy == "org", showsDivider: false)
            }
        }
    }

    @ViewBuilder
    private var retentionCard: some View {
        if let r = store.retention {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                CavnarKicker("People to keep \u{00B7} only you see this")
                if r.people.isEmpty {
                    Text("No warning signs on the schedule side.").cavnarText(.secondary)
                }
                ForEach(r.people.prefix(10)) { p in
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                            TonePill(text: p.level.capitalized, tone: p.level == "high" ? .bad : .warning)
                            Text(p.name).cavnarText(.label)
                            if let risk = p.risk { Text("\(Int((risk * 100).rounded()))%").cavnarText(.figureS) }
                        }
                        if !p.reasons.isEmpty { CavnarMixedText(p.reasons.joined(separator: "; "), role: .secondary) }
                        Button {
                            Task { await store.post(BenchActionBody(action: "retention_protect", name: p.name, protect: !p.protect),
                                                    key: "r\(p.name)", historyId: historyId) }
                        } label: {
                            Text(p.protect ? "Protecting their hours \u{00B7} stop" : "Protect their hours")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .disabled(store.busy != nil)
                    }
                }
                if let b = r.basis { Text(b).cavnarText(.caption) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }
}
