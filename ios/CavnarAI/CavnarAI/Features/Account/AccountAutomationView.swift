import SwiftUI
import Observation

/// Automation & trust — the phone half of the web Account cards the
/// automation audit added (the schedule draft and its day, auto-publish,
/// trusted orders and their day, the Monday plan, the send delay) and the moat audit's "why Cavnar AI stopped asking"
/// record behind each one. Every switch defaults off on the server and
/// only does anything once the owner's own record has earned it; this
/// screen shows that record next to the switch so the two are never read
/// apart.
struct AccountAutomationView: View {
    @State private var viewModel = AccountAutomationViewModel()
    @Environment(\.dismiss) private var dismiss
    /// "Days & send delay" open.
    @State private var showingTiming = false

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

                    // A load failure up top; a save's refusal sits under the
                    // control that was changed (re-audit M18).
                    if viewModel.errorKey == nil, let error = viewModel.errorMessage {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if viewModel.isLoading && !viewModel.loaded {
                        CavnarWorkingLine().padding(.vertical, 12)
                    } else {
                        switches
                        trust
                        // "What Cavnar AI remembers → Memory" left this
                        // sheet: Memory is its own row on Account, one
                        // entry point (iOS readability round [73]).
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Automation")
            .task { await viewModel.load() }
        }
    }

    /// A save's refusal, under the control it came from (re-audit M18).
    @ViewBuilder
    private func saveError(_ keys: String...) -> some View {
        if let key = viewModel.errorKey, keys.contains(key), let error = viewModel.errorMessage {
            Text(error).cavnarText(.secondary, color: .cavnarRedText)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 6)
        }
    }

    // Each switch's detail is one line (re-audit M18); the days and the send
    // delay sit behind "Days & send delay".
    private var switches: some View {
        AccountSection(kicker: "What runs on its own") {
            // Hidden where a scheduling tool is named, as on the web — the
            // draft never runs there.
            if let draft = viewModel.autoDraft, draft.externalTool.isEmpty {
                AccountSwitchRow(
                    label: "Draft next week's schedule",
                    detail: "Every \(draft.day), a draft only. On by default with a paid Labor plan.",
                    isOn: Binding(get: { draft.enabled }, set: { on in Task { await viewModel.setAutoDraft(on) } }),
                    busy: viewModel.saving == "auto_draft",
                    showsDivider: true
                )
                saveError("auto_draft")
            }
            AccountSwitchRow(
                label: "Publish the schedule \(viewModel.autoPublish?.day ?? "Friday")",
                detail: viewModel.autoPublish.map { p in
                    p.armed ? "Armed \u{2014} undo from Home until 11."
                            : "\(p.trust) of \(p.needed) unedited weeks; it arms itself."
                },
                isOn: Binding(get: { viewModel.autoPublish?.enabled ?? false },
                              set: { on in Task { await viewModel.setAutoPublish(on) } }),
                busy: viewModel.saving == "auto_publish",
                showsDivider: true
            )
            saveError("auto_publish")
            if let order = viewModel.autoOrder {
                AccountSwitchRow(
                    label: "Send trusted supplier orders",
                    detail: order.suppliersTrusted == 0
                        ? "No supplier trusted yet \u{2014} three sent orders earns one."
                        : "\(order.suppliersTrusted) supplier\(order.suppliersTrusted == 1 ? "" : "s") trusted; \(order.dayName) with an hour to undo.",
                    isOn: Binding(get: { order.enabled }, set: { on in Task { await viewModel.setAutoOrder(on) } }),
                    busy: viewModel.saving == "auto_order",
                    showsDivider: true
                )
                saveError("auto_order")
            }
            AccountSwitchRow(
                label: "Monday plan",
                detail: "Up to three actions on Home, Monday 7am.",
                isOn: Binding(get: { viewModel.weeklyPlan ?? false },
                              set: { on in Task { await viewModel.setWeeklyPlan(on) } }),
                busy: viewModel.saving == "weekly_plan",
                showsDivider: true
            )
            saveError("weekly_plan")
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.22)) { showingTiming.toggle() }
            } label: {
                AccountKVRow(label: "Days & send delay", showsDivider: showingTiming) {
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .rotationEffect(.degrees(showingTiming ? 180 : 0))
                        .accessibilityHidden(true)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(showingTiming ? "Expanded" : "Collapsed")
            if showingTiming { timing }
        }
    }

    /// The draft day, the order day and the send delay.
    @ViewBuilder
    private var timing: some View {
        // The draft's day (owner 9/27/26: Thursday was fixed).
        if let draft = viewModel.autoDraft, draft.externalTool.isEmpty {
            AccountKVRow(label: "Draft day") {
                Picker("", selection: Binding(get: { draft.weekday },
                                              set: { d in Task { await viewModel.setAutoDraftDay(d) } })) {
                    ForEach(0..<6, id: \.self) { d in Text(AccountAutomationViewModel.dayNames[d]).tag(d) }
                }
                .tint(Color.cavnarEmber)
                .disabled(viewModel.saving == "auto_draft_day")
            }
            saveError("auto_draft_day")
        }
        if let order = viewModel.autoOrder {
            AccountKVRow(label: "Order day") {
                Picker("", selection: Binding(get: { order.weekday ?? 0 },
                                              set: { d in Task { await viewModel.setAutoOrderDay(d) } })) {
                    ForEach(0..<7, id: \.self) { d in Text(AccountAutomationViewModel.dayNames[d]).tag(d) }
                }
                .tint(Color.cavnarEmber)
                .disabled(viewModel.saving == "auto_order_day")
            }
            saveError("auto_order_day")
        }
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
        Text("Replies, posts and orders wait this long, with an undo on Home.")
            .cavnarText(.secondary)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.bottom, 6)
        saveError("send_delay")
    }

    /// What went back to the owner stays here; the full record — every
    /// band, supplier and invoice count — is the web's (re-audit M18).
    private var trust: some View {
        AccountSection(kicker: "Why it stopped asking") {
            if let t = viewModel.trust {
                // What went back to the owner, and why — asked again rather
                // than an automation going quiet on its own (M1 trust_ledger).
                ForEach(Array(t.lapsed.enumerated()), id: \.offset) { _, lapse in
                    CavnarCaveat(title: "Back to you", detail: lapse.text)
                        .padding(.vertical, 6)
                }
                CavnarWebLinkRow(title: "Your record",
                                 subtitle: viewModel.earnedCount == 0 ? "Nothing earned yet \u{2014} each line is your own approvals, schedules and orders."
                                    : "\(viewModel.earnedCount) earned \u{2014} each line is your own approvals, schedules and orders.",
                                 path: "account/automation", actionLabel: "On the web")
            } else {
                Text("The record hasn't loaded.").cavnarText(.body).padding(.vertical, 9)
            }
        }
    }
}

// MARK: - View model

@Observable
@MainActor
final class AccountAutomationViewModel {
    /// 0 = Monday, as the server's weekday numbers.
    static let dayNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    struct AutoDraft: Decodable {
        let ok: Bool
        let enabled: Bool
        let weekday: Int
        let day: String
        let externalTool: String
        enum CodingKeys: String, CodingKey { case ok, enabled, weekday, day; case externalTool = "external_tool" }
    }
    struct AutoPublish: Decodable {
        let ok: Bool; let enabled: Bool; let trust: Int; let needed: Int; let armed: Bool
        /// The day it goes: the day after the draft day.
        let day: String?
    }
    struct AutoOrderSupplier: Decodable { let name: String?; let trusted: Bool?; let orders: Int? }
    struct AutoOrder: Decodable {
        let ok: Bool
        let enabled: Bool
        let suppliers: [AutoOrderSupplier]?
        let weekday: Int?
        let day: String?
        var suppliersTrusted: Int { (suppliers ?? []).filter { $0.trusted ?? false }.count }
        var dayName: String { day ?? "Monday" }
    }
    struct WeeklyPlan: Decodable { let ok: Bool; let enabled: Bool }
    struct SendDelay: Decodable { let ok: Bool; let minutes: Int; let choices: [Int] }
    /// One reply band's record (models.auto_approve_trust): when it earned
    /// trust, the lapse that took it back, and how much of the record is
    /// auto-posts left standing a week (each counts half) — memory round
    /// 9/29/26, M1 trust_ledger. All lenient.
    struct TrustBand: Decodable {
        let trusted: Bool?
        var earnedAt: String? = nil
        var lapsed: Lapse? = nil
        var weakCredit: Double? = nil
        struct Lapse: Decodable { let at: String?; let reason: String? }
        enum CodingKeys: String, CodingKey {
            case trusted, lapsed
            case earnedAt = "earned_at"
            case weakCredit = "weak_credit"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            trusted = (try? c.decodeIfPresent(Bool.self, forKey: .trusted)) ?? nil
            earnedAt = (try? c.decodeIfPresent(String.self, forKey: .earnedAt)) ?? nil
            lapsed = (try? c.decodeIfPresent(Lapse.self, forKey: .lapsed)) ?? nil
            weakCredit = (try? c.decodeIfPresent(Double.self, forKey: .weakCredit)) ?? nil
        }
    }
    struct TrustSchedule: Decodable {
        let enabled: Bool?; let uneditedInARow: Int; let needed: Int
        /// When the owner last undid an automatic publish (M/D/YY): the
        /// clean runs count again from there (M1 "undo").
        var undoneOn: String? = nil
        enum CodingKeys: String, CodingKey {
            case enabled, needed
            case uneditedInARow = "unedited_in_a_row"
            case undoneOn = "undone_on"
        }
    }
    struct TrustAutoApprove: Decodable { let enabled: Bool?; let bands: [String: TrustBand]? }
    struct TrustSupplier: Decodable {
        let name: String?; let trusted: Bool?; let orders: Int?; let needed: Int?
        /// The last undone automatic order to this supplier, and the clean
        /// orders since (ordering.supplier_trust — M1 "undo").
        var undoneAt: String? = nil
        var cleanSinceUndo: Int? = nil
        enum CodingKeys: String, CodingKey {
            case name, trusted, orders, needed
            case undoneAt = "undone_at"
            case cleanSinceUndo = "clean_since_undo"
        }
    }
    /// An automation whose trust lapsed and has not been earned back —
    /// what the owner is re-asked about (automation_trust.lapsed_items).
    struct TrustLapse: Decodable, Hashable {
        let text: String
        var lapsedOn: String? = nil
        enum CodingKeys: String, CodingKey { case text; case lapsedOn = "lapsed_on" }
    }
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
        var lapsed: [TrustLapse] = []
        enum CodingKeys: String, CodingKey {
            case ok, schedule, suppliers, invoices, lapsed
            case autoApprove = "auto_approve"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
            autoApprove = (try? c.decodeIfPresent(TrustAutoApprove.self, forKey: .autoApprove)) ?? nil
            schedule = (try? c.decodeIfPresent(TrustSchedule.self, forKey: .schedule)) ?? nil
            suppliers = (try? c.decodeIfPresent([TrustSupplier].self, forKey: .suppliers)) ?? []
            invoices = (try? c.decodeIfPresent([TrustInvoice].self, forKey: .invoices)) ?? []
            lapsed = (try? c.decodeIfPresent([TrustLapse].self, forKey: .lapsed)) ?? []
        }
        var earnedBands: [String] {
            (autoApprove?.bands ?? [:]).filter { $0.value.trusted ?? false }.keys.sorted(by: >).map { "\($0)★" }
        }

        /// "5★ since 9/2/26 · 4★ since 9/10/26 · 1.5 of the record is
        /// auto-posts left standing a week (each counts half)" — nil with
        /// nothing earned.
        var bandsDetail: String? {
            let bands = (autoApprove?.bands ?? [:]).filter { $0.value.trusted ?? false }
                .sorted { $0.key > $1.key }
            guard !bands.isEmpty else { return nil }
            var parts = bands.compactMap { k, b in b.earnedAt.map { "\(k)★ since \(CavnarDate.mdyLocal($0))" } }
            let weak = bands.map { $0.value.weakCredit ?? 0 }.reduce(0, +)
            if weak > 0 {
                let w = weak == weak.rounded() ? String(Int(weak)) : String(format: "%.1f", weak)
                parts.append("\(w) of the record is auto-posts left standing a week (each counts half)")
            }
            return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
        }
    }
    private struct EnabledBody: Encodable { let enabled: Bool }
    private struct WeekdayBody: Encodable { let weekday: Int }
    private struct MinutesBody: Encodable { let minutes: Int }


    var autoDraft: AutoDraft?
    var autoPublish: AutoPublish?
    var autoOrder: AutoOrder?
    var weeklyPlan: Bool?
    var sendDelay: SendDelay?
    var trust: Trust?
    var isLoading = false
    var loaded = false
    var saving: String?
    var errorMessage: String?
    /// Which control's save failed — its message shows under that control;
    /// nil for a load failure (re-audit M18).
    var errorKey: String?

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
        autoDraft = try? await client.send("/mobile/api/labor/auto-draft", hapticOnError: false)
        autoPublish = try? await client.send("/mobile/api/labor/auto-publish", hapticOnError: false)
        autoOrder = try? await client.send("/mobile/api/food-cost/auto-order", hapticOnError: false)
        if let wp: WeeklyPlan = try? await client.send("/mobile/api/labor/weekly-plan", hapticOnError: false) {
            weeklyPlan = wp.enabled
        }
        sendDelay = try? await client.send("/mobile/api/account/send-delay", hapticOnError: false)
        trust = try? await client.send("/mobile/api/account/trust", hapticOnError: false)
        if autoPublish == nil && sendDelay == nil {
            errorMessage = "Couldn't load these settings."
        }
    }

    func setAutoDraft(_ on: Bool) async {
        await save("auto_draft") {
            self.autoDraft = try await self.client.send("/mobile/api/labor/auto-draft", method: .post, body: EnabledBody(enabled: on))
        }
    }

    /// The publish day follows the draft day, so it is read again.
    func setAutoDraftDay(_ weekday: Int) async {
        await save("auto_draft_day") {
            self.autoDraft = try await self.client.send("/mobile/api/labor/auto-draft", method: .post, body: WeekdayBody(weekday: weekday))
            self.autoPublish = try? await self.client.send("/mobile/api/labor/auto-publish", hapticOnError: false)
        }
    }

    func setAutoOrderDay(_ weekday: Int) async {
        await save("auto_order_day") {
            self.autoOrder = try await self.client.send("/mobile/api/food-cost/auto-order", method: .post, body: WeekdayBody(weekday: weekday))
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

    private func save(_ key: String, _ work: () async throws -> Void) async {
        saving = key; errorMessage = nil; errorKey = nil
        defer { saving = nil }
        do {
            try await work()
            Haptic.selection()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            errorKey = key
        } catch {
            errorMessage = "Couldn't save that."
            errorKey = key
        }
    }
}
