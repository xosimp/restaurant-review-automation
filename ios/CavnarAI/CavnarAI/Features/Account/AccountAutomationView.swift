import SwiftUI
import Observation

/// Automation & trust — the phone half of the web Account cards the
/// automation audit added (auto-publish, trusted orders, the Monday plan,
/// the send delay) and the moat audit's "why Cavnar AI stopped asking"
/// record behind each one. Every switch defaults off on the server and
/// only does anything once the owner's own record has earned it; this
/// screen shows that record next to the switch so the two are never read
/// apart.
struct AccountAutomationView: View {
    @State private var viewModel = AccountAutomationViewModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Automation") {
                        Image(systemName: "sparkles.rectangle.stack")
                            .font(.system(size: 26, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(width: 52, height: 52)
                            .background(Color.cavnarEmber.opacity(0.12))
                            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                    } subtitle: {
                        Text(viewModel.earnedCount == 0 ? "Nothing runs on its own until your record earns it."
                             : "\(viewModel.earnedCount) earned from your own record.")
                    }

                    if viewModel.isLoading && !viewModel.loaded {
                        CavnarWorkingLine().padding(.vertical, 12)
                    } else {
                        switches
                        trust
                        memory
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Automation")
            .task { await viewModel.load() }
        }
    }

    private var switches: some View {
        AccountSection(kicker: "What runs on its own") {
            AccountSwitchRow(
                label: "Publish the schedule Friday",
                detail: viewModel.autoPublish.map { p in
                    p.armed ? "Armed — \(p.trust) schedules went out unedited in a row. You're told at 9, and can undo from Home until 11."
                            : "Your record: \(p.trust) of \(p.needed) unedited schedules in a row. It arms itself when you get there."
                },
                isOn: Binding(get: { viewModel.autoPublish?.enabled ?? false },
                              set: { on in Task { await viewModel.setAutoPublish(on) } }),
                busy: viewModel.saving == "auto_publish",
                showsDivider: true
            )
            if let order = viewModel.autoOrder {
                AccountSwitchRow(
                    label: "Send trusted supplier orders",
                    detail: order.suppliersTrusted == 0
                        ? "No supplier trusted yet — three sent orders earns one. A draft inside your usual total then goes Monday with an hour to undo."
                        : "\(order.suppliersTrusted) supplier\(order.suppliersTrusted == 1 ? "" : "s") trusted. A draft inside your usual total goes Monday 8am with an hour to undo.",
                    isOn: Binding(get: { order.enabled }, set: { on in Task { await viewModel.setAutoOrder(on) } }),
                    busy: viewModel.saving == "auto_order",
                    showsDivider: true
                )
            }
            AccountSwitchRow(
                label: "Monday plan",
                detail: "Monday 7am the assistant reads the week and files up to three actions on Home. Nobody is texted.",
                isOn: Binding(get: { viewModel.weeklyPlan ?? false },
                              set: { on in Task { await viewModel.setWeeklyPlan(on) } }),
                busy: viewModel.saving == "weekly_plan",
                showsDivider: true
            )
            AccountKVRow(label: "Send delay", showsDivider: false) {
                Picker("", selection: Binding(get: { viewModel.sendDelay?.minutes ?? 0 },
                                              set: { m in Task { await viewModel.setSendDelay(m) } })) {
                    ForEach(viewModel.sendDelay?.choices ?? [0], id: \.self) { m in
                        Text(m == 0 ? "None" : "\(m) min").tag(m)
                    }
                }
                .tint(Color.cavnarEmber)
                .disabled(viewModel.saving == "send_delay")
            }
            Text("Every reply, post and order waits this long before it goes, with an undo on Home.")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 6)
        }
    }

    private var trust: some View {
        AccountSection(kicker: "Why it stopped asking") {
            if let t = viewModel.trust {
                AccountKVRow(label: "Review replies") {
                    AccountPill(text: t.earnedBands.isEmpty ? "Not yet" : "Trusted on " + t.earnedBands.joined(separator: ", "),
                                on: !t.earnedBands.isEmpty)
                }
                AccountKVRow(label: "Schedule publishing", showsDivider: !t.suppliers.isEmpty || !t.invoices.isEmpty) {
                    AccountPill(text: t.schedule.map { $0.uneditedInARow >= $0.needed ? "Earned" : "\($0.uneditedInARow) of \($0.needed)" } ?? "—",
                                on: (t.schedule?.uneditedInARow ?? 0) >= (t.schedule?.needed ?? 1))
                }
                ForEach(Array(t.suppliers.enumerated()), id: \.offset) { i, s in
                    AccountKVRow(label: "Orders to \(s.name ?? "supplier")",
                                 showsDivider: i < t.suppliers.count - 1 || !t.invoices.isEmpty) {
                        AccountPill(text: (s.trusted ?? false) ? "Earned" : "\(s.orders ?? 0) of \((s.orders ?? 0) + (s.needed ?? 0))",
                                    on: s.trusted ?? false)
                    }
                }
                ForEach(Array(t.invoices.enumerated()), id: \.offset) { i, v in
                    AccountKVRow(label: "Invoices from \(v.supplier ?? "supplier")", showsDivider: i < t.invoices.count - 1) {
                        AccountPill(text: (v.trusted ?? false) ? "Earned" : "\(v.fullAccepts ?? 0) of \((v.fullAccepts ?? 0) + (v.needed ?? 0))",
                                    on: v.trusted ?? false)
                    }
                }
                Text("Each line is your own record — approved replies you didn't edit, schedules you didn't change, orders you sent, scans you applied as read. Nothing is inferred.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 8)
                    .padding(.bottom, 6)
            } else {
                Text("The record hasn't loaded.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3).padding(.vertical, 9)
            }
        }
    }
}

extension AccountAutomationView {
    /// What Ask remembers about the restaurant (GET /account/memory), each
    /// fact with a red ✕ to forget it (POST /account/memory/forget) — the
    /// web's "What Cavnar AI remembers" card. Adding a fact stays on the web
    /// and in Ask ("remember that…").
    fileprivate var memory: some View {
        AccountSection(kicker: "What Cavnar AI remembers") {
            if let facts = viewModel.memory {
                if facts.isEmpty {
                    Text("Nothing remembered yet. Tell Ask Cavnar AI \u{201C}remember that\u{2026}\u{201D} and it appears here.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.vertical, 9)
                }
                ForEach(Array(facts.enumerated()), id: \.element.id) { i, fact in
                    AccountActionRow(
                        label: fact.fact,
                        detail: fact.detailLine,
                        symbol: "xmark",
                        tone: .cavnarRed,
                        busy: viewModel.forgetting == fact.fact,
                        showsDivider: i < facts.count - 1
                    ) { Task { await viewModel.forget(fact) } }
                    .accessibilityHint("Forget this")
                }
                Text("These shape every answer. Anyone on this account can add one in Ask; you can forget any of them here.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 8)
                    .padding(.bottom, 6)
            } else {
                Text("The memory hasn't loaded.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3).padding(.vertical, 9)
            }
        }
    }
}

// MARK: - View model

@Observable
@MainActor
final class AccountAutomationViewModel {
    struct AutoPublish: Decodable { let ok: Bool; let enabled: Bool; let trust: Int; let needed: Int; let armed: Bool }
    struct AutoOrderSupplier: Decodable { let name: String?; let trusted: Bool?; let orders: Int? }
    struct AutoOrder: Decodable {
        let ok: Bool
        let enabled: Bool
        let suppliers: [AutoOrderSupplier]?
        var suppliersTrusted: Int { (suppliers ?? []).filter { $0.trusted ?? false }.count }
    }
    struct WeeklyPlan: Decodable { let ok: Bool; let enabled: Bool }
    struct SendDelay: Decodable { let ok: Bool; let minutes: Int; let choices: [Int] }
    struct TrustBand: Decodable { let trusted: Bool? }
    struct TrustSchedule: Decodable {
        let enabled: Bool?; let uneditedInARow: Int; let needed: Int
        enum CodingKeys: String, CodingKey { case enabled, needed; case uneditedInARow = "unedited_in_a_row" }
    }
    struct TrustAutoApprove: Decodable { let enabled: Bool?; let bands: [String: TrustBand]? }
    struct TrustSupplier: Decodable { let name: String?; let trusted: Bool?; let orders: Int?; let needed: Int? }
    struct TrustInvoice: Decodable {
        let supplier: String?; let trusted: Bool?; let fullAccepts: Int?; let needed: Int?
        enum CodingKeys: String, CodingKey { case supplier, trusted, needed; case fullAccepts = "full_accepts" }
    }
    struct Trust: Decodable {
        let ok: Bool
        let autoApprove: TrustAutoApprove?
        let schedule: TrustSchedule?
        let suppliers: [TrustSupplier]
        let invoices: [TrustInvoice]
        enum CodingKeys: String, CodingKey { case ok, schedule, suppliers, invoices; case autoApprove = "auto_approve" }
        var earnedBands: [String] {
            (autoApprove?.bands ?? [:]).filter { $0.value.trusted ?? false }.keys.sorted(by: >).map { "\($0)★" }
        }
    }
    private struct EnabledBody: Encodable { let enabled: Bool }
    private struct MinutesBody: Encodable { let minutes: Int }

    /// One remembered fact, as GET /account/memory lists it.
    struct MemoryFact: Decodable, Identifiable, Equatable {
        let fact: String
        let kind: String?
        let source: String?
        let createdAt: String?
        var id: String { fact }
        enum CodingKeys: String, CodingKey { case fact, kind, source; case createdAt = "created_at" }

        /// "context · from Ask · 9/21/26"
        var detailLine: String {
            var parts = [kind ?? "context"]
            if let source, !source.isEmpty { parts.append("from \(source)") }
            if let createdAt, !createdAt.isEmpty { parts.append(CavnarDate.mdyLocal(createdAt)) }
            return parts.joined(separator: " \u{00B7} ")
        }
    }
    private struct MemoryResponse: Decodable { let ok: Bool; let facts: [MemoryFact] }
    private struct FactBody: Encodable { let fact: String }

    var autoPublish: AutoPublish?
    var autoOrder: AutoOrder?
    var weeklyPlan: Bool?
    var sendDelay: SendDelay?
    var trust: Trust?
    /// Nil until it loads (or when it could not).
    var memory: [MemoryFact]?
    /// The fact being forgotten right now.
    var forgetting: String?
    var isLoading = false
    var loaded = false
    var saving: String?
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    var earnedCount: Int {
        var n = 0
        if let t = trust {
            n += t.earnedBands.isEmpty ? 0 : 1
            if let s = t.schedule, s.uneditedInARow >= s.needed { n += 1 }
            n += t.suppliers.filter { $0.trusted ?? false }.count
            n += t.invoices.filter { $0.trusted ?? false }.count
        }
        return n
    }

    func load() async {
        isLoading = true
        defer { isLoading = false; loaded = true }
        // Each is its own read and its own failure: a login without Food
        // Cost gets a 403 on auto-order, which simply hides that row.
        autoPublish = try? await client.send("/mobile/api/labor/auto-publish", hapticOnError: false)
        autoOrder = try? await client.send("/mobile/api/food-cost/auto-order", hapticOnError: false)
        if let wp: WeeklyPlan = try? await client.send("/mobile/api/labor/weekly-plan", hapticOnError: false) {
            weeklyPlan = wp.enabled
        }
        sendDelay = try? await client.send("/mobile/api/account/send-delay", hapticOnError: false)
        trust = try? await client.send("/mobile/api/account/trust", hapticOnError: false)
        if let m: MemoryResponse = try? await client.send("/mobile/api/account/memory", hapticOnError: false) {
            memory = m.facts
        }
        if autoPublish == nil && sendDelay == nil {
            errorMessage = "Couldn't load these settings."
        }
    }

    func setAutoPublish(_ on: Bool) async {
        await save("auto_publish") {
            self.autoPublish = try await self.client.send("/mobile/api/labor/auto-publish", method: .post, body: EnabledBody(enabled: on))
        }
    }

    func setAutoOrder(_ on: Bool) async {
        await save("auto_order") {
            self.autoOrder = try await self.client.send("/mobile/api/food-cost/auto-order", method: .post, body: EnabledBody(enabled: on))
        }
    }

    func setWeeklyPlan(_ on: Bool) async {
        await save("weekly_plan") {
            let wp: WeeklyPlan = try await self.client.send("/mobile/api/labor/weekly-plan", method: .post, body: EnabledBody(enabled: on))
            self.weeklyPlan = wp.enabled
        }
    }

    func setSendDelay(_ minutes: Int) async {
        await save("send_delay") {
            self.sendDelay = try await self.client.send("/mobile/api/account/send-delay", method: .post, body: MinutesBody(minutes: minutes))
        }
    }

    /// Forget one fact. The row leaves once the server confirms it.
    func forget(_ fact: MemoryFact) async {
        forgetting = fact.fact; errorMessage = nil
        defer { forgetting = nil }
        do {
            let _: APIClient.EmptyResponse = try await client.send(
                "/mobile/api/account/memory/forget", method: .post, body: FactBody(fact: fact.fact))
            memory?.removeAll { $0 == fact }
            Haptic.success()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't forget that."
        }
    }

    private func save(_ key: String, _ work: () async throws -> Void) async {
        saving = key; errorMessage = nil
        defer { saving = nil }
        do {
            try await work()
            Haptic.selection()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't save that."
        }
    }
}
