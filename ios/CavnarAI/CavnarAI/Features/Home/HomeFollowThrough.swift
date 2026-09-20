import SwiftUI
import Observation

/// Home's follow-through: what is still open, where the goals stand, what
/// the owner's own changes did, and the close-out handoff.
///
/// All of it existed on the web and none of it on the phone, which is the
/// device an owner actually has on the floor — so the accountability loop
/// (an issue with a name against it, a result that landed, tonight's
/// handoff) could only be closed at a desk. Same endpoints as the web:
/// /mobile/api/actions, /goals, /outcomes, /closeout.

// MARK: - Models

struct ActionItem: Decodable, Identifiable {
    struct Action: Decodable {
        /// The same endpoint on each client — the web and mobile APIs have
        /// different prefixes, so the server hands over both.
        struct Route: Decodable { let web: String?; let mobile: String? }
        let label: String
        let route: Route?
        let method: String?
        let module: String?
    }
    let key: String
    let kind: String
    let title: String
    let detail: String?
    let severity: String
    let action: Action?
    var id: String { key }

    /// Home's own severity vocabulary, so these rows read exactly like the
    /// needs-attention ones above them.
    var tone: Color {
        switch severity {
        case "critical": return .cavnarRed
        case "important": return .cavnarAmber
        default: return .cavnarInk3
        }
    }
}

struct GoalRow: Decodable, Identifiable {
    let id: Int
    let summary: String?
    let label: String?
    let state: String?

    var tone: Color {
        switch state {
        case "met", "moving_right_way": return .cavnarGreen
        case "moving_wrong_way", "missed": return .cavnarRed
        default: return .cavnarInk3
        }
    }
}

struct ResultRow: Decodable, Identifiable {
    let id: Int
    let summary: String?
    let verdict: String?

    var tone: Color {
        switch verdict {
        case "improved": return .cavnarGreen
        case "worsened": return .cavnarRed
        default: return .cavnarInk3
        }
    }
}

struct CloseOutEntry: Decodable {
    let businessDate: String?
    let submittedBy: String?
    let wentWell: String?
    let wentWrong: String?
    let eightySixed: String?
    let callouts: String?

    enum CodingKeys: String, CodingKey {
        case businessDate = "business_date"
        case submittedBy = "submitted_by"
        case wentWell = "went_well"
        case wentWrong = "went_wrong"
        case eightySixed = "eighty_sixed"
        case callouts = "callouts"
    }
}

// MARK: - View model

@Observable
@MainActor
final class HomeFollowThroughViewModel {
    var actions: [ActionItem] = []
    var goals: [GoalRow] = []
    var results: [ResultRow] = []
    var closeOut: CloseOutEntry?
    var closeOutDate: String?
    var caveat: String?
    var value: ValueSummary?
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct ActionsResponse: Decodable { let ok: Bool; let items: [ActionItem] }
    private struct GoalsResponse: Decodable { let ok: Bool; let goals: [GoalRow] }
    private struct OutcomesResponse: Decodable {
        let ok: Bool
        let outcomes: [ResultRow]
        let caveat: String?
    }
    private struct CloseOutResponse: Decodable {
        let ok: Bool
        let closeout: CloseOutEntry?
        let businessDate: String?
        enum CodingKeys: String, CodingKey {
            case ok, closeout
            case businessDate = "business_date"
        }
    }
    private struct OKResponse: Decodable { let ok: Bool; let error: String? }

    /// GET /mobile/api/value. Four figures that are never added to each
    /// other — see value_delivered.py. Decoded leniently: an older backend
    /// that does not serve this route leaves `value` nil and the section
    /// simply does not appear.
    struct ValueSummary: Decodable {
        struct Delivered: Decodable {
            let monthly: Double?
            let annual: Double?
            let wins: Int?
            let evaluated: Int?
            let inFlight: Int?
            let unmeasurable: Int?
            let noClearChange: Int?
            let biggest: Biggest?
            let caveat: String?
            struct Biggest: Decodable { let title: String?; let monthly: Double?; let summary: String? }
            enum CodingKeys: String, CodingKey {
                case monthly, annual, wins, evaluated, biggest, caveat
                case inFlight = "in_flight"
                case unmeasurable
                case noClearChange = "no_clear_change"
            }
        }
        struct Avoided: Decodable {
            struct Item: Decodable {
                let key: String?
                let label: String?
                let dollars: Double?
                let hours: Double?
                let rate: String?
                let basis: String?
            }
            let items: [Item]?
            let dollars: Double?
            let hours: Double?
        }
        struct Opportunity: Decodable { let monthly: Double? }
        struct Surfaced: Decodable { let dollars: Double?; let alerts: Int?; let days: Int? }
        let ok: Bool
        let delivered: Delivered?
        let avoided: Avoided?
        let opportunity: Opportunity?
        let surfaced: Surfaced?
    }

    /// Every block is optional: a login without an endpoint's permission, or
    /// a module it can't see, simply shows fewer rows rather than an error.
    func load() async {
        isLoading = actions.isEmpty && goals.isEmpty && results.isEmpty
        defer { isLoading = false }
        async let a: ActionsResponse? = try? client.send("/mobile/api/actions", hapticOnError: false)
        async let g: GoalsResponse? = try? client.send("/mobile/api/goals", hapticOnError: false)
        async let o: OutcomesResponse? = try? client.send("/mobile/api/outcomes", hapticOnError: false)
        async let c: CloseOutResponse? = try? client.send("/mobile/api/closeout", hapticOnError: false)
        async let v: ValueSummary? = try? client.send("/mobile/api/value", hapticOnError: false)
        actions = (await a)?.items ?? []
        goals = (await g)?.goals ?? []
        let outcomes = await o
        results = (outcomes?.outcomes ?? []).filter { $0.summary?.isEmpty == false }
        caveat = outcomes?.caveat
        let close = await c
        closeOut = close?.closeout
        closeOutDate = close?.businessDate
        value = await v
    }

    private struct SnoozeBody: Encodable { let key: String }

    func snooze(_ item: ActionItem) async {
        actions.removeAll { $0.key == item.key }       // it's back tomorrow
        _ = try? await client.send("/mobile/api/actions/snooze", method: .post,
                                   body: SnoozeBody(key: item.key),
                                   hapticOnError: false) as OKResponse
        await load()
    }

    func run(_ item: ActionItem) async {
        guard let route = item.action?.route?.mobile else { return }
        let done: OKResponse? = try? await client.send(route, method: .post, retryTransient: false)
        if done?.ok == true { await Haptic.success() }
        await load()
    }

    private struct CloseOutBody: Encodable {
        let wentWell: String
        let wentWrong: String
        let eightySixed: String
        let callouts: String
        enum CodingKeys: String, CodingKey {
            case wentWell = "went_well"
            case wentWrong = "went_wrong"
            case eightySixed = "eighty_sixed"
            case callouts = "callouts"
        }
    }

    @discardableResult
    func saveCloseOut(wentWell: String, wentWrong: String, eightySixed: String,
                      callouts: String) async -> Bool {
        errorMessage = nil
        do {
            let r: OKResponse = try await client.send(
                "/mobile/api/closeout", method: .post,
                body: CloseOutBody(wentWell: wentWell, wentWrong: wentWrong,
                                   eightySixed: eightySixed, callouts: callouts),
                retryTransient: false)
            guard r.ok else {
                errorMessage = r.error ?? "Couldn't save that."
                return false
            }
            await load()
            return true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't save that."
            return false
        }
    }
}

// MARK: - Section

struct HomeFollowThrough: View {
    let viewModel: HomeFollowThroughViewModel
    var onOpenModule: (String) -> Void
    @State private var showingCloseOut = false

    private var hasAnything: Bool {
        !viewModel.actions.isEmpty || !viewModel.goals.isEmpty || !viewModel.results.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if !viewModel.actions.isEmpty {
                HomeSectionHeader(kicker: "Follow-through", title: "Still open",
                                  trailing: "\(viewModel.actions.count)")
                VStack(spacing: 0) {
                    ForEach(Array(viewModel.actions.enumerated()), id: \.element.id) { index, item in
                        actionRow(item, showsDivider: index < viewModel.actions.count - 1)
                    }
                }
                .cavnarCard()
            }

            if !viewModel.goals.isEmpty {
                HomeSectionHeader(kicker: "Where you're heading", title: "Goals")
                VStack(spacing: 0) {
                    ForEach(Array(viewModel.goals.enumerated()), id: \.element.id) { index, goal in
                        lineRow(goal.summary ?? goal.label ?? "", tone: goal.tone,
                                showsDivider: index < viewModel.goals.count - 1)
                    }
                }
                .cavnarCard()
            }

            if !viewModel.results.isEmpty {
                HomeSectionHeader(kicker: "Measured", title: "What your changes did")
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(viewModel.results.enumerated()), id: \.element.id) { index, r in
                        lineRow(r.summary ?? "", tone: r.tone, showsDivider: index < viewModel.results.count - 1)
                    }
                    if let caveat = viewModel.caveat {
                        CavnarCaveat(title: "Before and after, not proof", detail: caveat)
                            .padding(.top, 10)
                    }
                }
                .cavnarCard()
            }

            valueCard

            closeOutCard
        }
        .task { await viewModel.load() }
        .sheet(isPresented: $showingCloseOut) {
            CloseOutSheet(viewModel: viewModel)
        }
    }

    /// What Cavnar AI has been worth. Four figures, never added together —
    /// a measurement, an estimate at stated rates, an alert total and a gap
    /// against target are not addends (see value_delivered.py).
    @ViewBuilder
    private var valueCard: some View {
        if let v = viewModel.value, let d = v.delivered,
           (d.wins ?? 0) > 0 || (d.inFlight ?? 0) > 0
            || (v.avoided?.hours ?? 0) > 0 || (v.opportunity?.monthly ?? 0) > 0 {
            HomeSectionHeader(kicker: "Worth", title: "What Cavnar AI has been worth")
            VStack(alignment: .leading, spacing: 0) {
                if (d.wins ?? 0) > 0 {
                    lineRow(Self.deliveredLine(d), tone: .cavnarGreen, showsDivider: true)
                    if let big = d.biggest?.summary {
                        lineRow("Biggest so far: " + big, tone: .cavnarInk3, showsDivider: true)
                    }
                } else {
                    lineRow(Self.nothingMeasuredLine(d), tone: .cavnarInk3, showsDivider: true)
                }
                if let denom = Self.denominatorLine(d) {
                    lineRow(denom, tone: .cavnarInk3, showsDivider: true)
                }
                if let av = v.avoided, let line = Self.avoidedLine(av) {
                    lineRow(line, tone: .cavnarInk3, showsDivider: true)
                }
                if let sf = v.surfaced, let dollars = sf.dollars, dollars > 0 {
                    lineRow("\(Self.money(dollars)) of problems put in front of you across "
                            + "\(sf.alerts ?? 0) alerts in the last \(sf.days ?? 30) days",
                            tone: .cavnarInk3, showsDivider: true)
                }
                if let op = v.opportunity?.monthly, op > 0 {
                    lineRow("\(Self.money(op))/month still on the table — available, not captured",
                            tone: .cavnarEmber, showsDivider: false)
                }
                if let caveat = d.caveat {
                    CavnarCaveat(title: "Before and after, not proof", detail: caveat)
                        .padding(.top, 10)
                }
            }
            .cavnarCard()
        }
    }

    private static func money(_ v: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        return "$" + (f.string(from: NSNumber(value: v)) ?? String(Int(v)))
    }

    private static func deliveredLine(_ d: HomeFollowThroughViewModel.ValueSummary.Delivered) -> String {
        let wins = d.wins ?? 0
        var s = "\(money(d.monthly ?? 0))/month measured across \(wins) change"
        if wins != 1 { s += "s" }
        if let annual = d.annual, annual > 0 {
            s += " — about \(money(annual)) a year if it holds"
        }
        return s + "."
    }

    private static func nothingMeasuredLine(_ d: HomeFollowThroughViewModel.ValueSummary.Delivered) -> String {
        let n = d.inFlight ?? 0
        if n > 0 {
            return "Nothing measured yet. \(n) change\(n == 1 ? "" : "s") being measured now."
        }
        return "Nothing measured yet. Track a recommendation and its result lands here."
    }

    /// The denominator always travels with the total: "2 results" reads
    /// differently when 8 others came back unreadable.
    private static func denominatorLine(_ d: HomeFollowThroughViewModel.ValueSummary.Delivered) -> String? {
        let evaluated = d.evaluated ?? 0
        let unknown = d.unmeasurable ?? 0
        let flat = d.noClearChange ?? 0
        guard evaluated > 0, unknown > 0 || flat > 0 else { return nil }
        var bits: [String] = []
        if flat > 0 { bits.append("\(flat) showed no clear change") }
        if unknown > 0 { bits.append("\(unknown) couldn't be measured") }
        return "Of \(evaluated) finished: " + bits.joined(separator: ", ") + "."
    }

    private static func avoidedLine(_ av: HomeFollowThroughViewModel.ValueSummary.Avoided) -> String? {
        var bits: [String] = []
        if let h = av.hours, h > 0 { bits.append("\(Int(h.rounded())) hours of work done for you") }
        if let d = av.dollars, d > 0 { bits.append("\(money(d)) you'd otherwise have paid for") }
        guard !bits.isEmpty else { return nil }
        return bits.joined(separator: " · ") + " (estimates at stated rates)"
    }

    private func actionRow(_ item: ActionItem, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Circle().fill(item.tone).frame(width: 8, height: 8).padding(.top, 6)
                VStack(alignment: .leading, spacing: 3) {
                    HomeMixedText.make(item.title, size: 15, weight: 600, color: .cavnarInk)
                    if let detail = item.detail {
                        HomeMixedText.make(detail, size: 12.5, weight: 500, color: .cavnarInk3)
                    }
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 6) {
                    if let action = item.action {
                        Button {
                            Haptic.light()
                            if action.route?.mobile != nil {
                                Task { await viewModel.run(item) }
                            } else if let module = action.module {
                                onOpenModule(module)
                            }
                        } label: {
                            Text(action.label)
                                .font(.cavnarBody(13, weight: 600))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .buttonStyle(.plain)
                    }
                    Button {
                        Haptic.light()
                        Task { await viewModel.snooze(item) }
                    } label: {
                        Text("Not today")
                            .font(.cavnarBody(12.5))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Puts it back tomorrow")
                }
            }
            .padding(.vertical, 11)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }

    private func lineRow(_ text: String, tone: Color, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Circle().fill(tone).frame(width: 8, height: 8).padding(.top, 6)
                HomeMixedText.make(text, size: 14.5, weight: 500, color: .cavnarInk2)
                Spacer(minLength: 0)
            }
            .padding(.vertical, 11)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }

    private var closeOutCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "End of the night", title: "Close-out",
                              trailing: viewModel.closeOut == nil ? nil : "filed")
            VStack(alignment: .leading, spacing: 10) {
                Text(viewModel.closeOut == nil
                     ? "Four lines from whoever closes. They lead tomorrow morning's brief."
                     : filedSummary)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                Button {
                    Haptic.light()
                    showingCloseOut = true
                } label: {
                    Text(viewModel.closeOut == nil ? "Hand off the night" : "Update the handoff")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
            .cavnarCard()
        }
    }

    private var filedSummary: String {
        guard let c = viewModel.closeOut else { return "" }
        let who = c.submittedBy ?? "A manager"
        let bits = [c.wentWrong, c.eightySixed.map { "86'd: \($0)" }, c.callouts.map { "callouts: \($0)" }]
            .compactMap { $0 }
        return bits.isEmpty ? "\(who) filed tonight's handoff." : "\(who): " + bits.joined(separator: " · ")
    }
}

// MARK: - The handoff itself

private enum CloseOutField: Hashable, CaseIterable { case well, wrong, eightySix, callouts }

struct CloseOutSheet: View {
    let viewModel: HomeFollowThroughViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var wentWell = ""
    @State private var wentWrong = ""
    @State private var eightySixed = ""
    @State private var callouts = ""
    @State private var postedLabel: String?
    @FocusState private var focused: CloseOutField?

    private var canSubmit: Bool {
        ![wentWell, wentWrong, eightySixed, callouts]
            .allSatisfy { $0.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("The numbers from tonight land at 3am. This is the only account of what "
                         + "actually happened — it leads tomorrow morning's brief.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    AccountSection(kicker: viewModel.closeOutDate ?? "Tonight") {
                        AccountField(label: "What went well", text: $wentWell,
                                     focus: $focused, field: CloseOutField.well)
                        AccountField(label: "What went wrong", text: $wentWrong,
                                     focus: $focused, field: CloseOutField.wrong)
                        AccountField(label: "What we ran out of", text: $eightySixed,
                                     focus: $focused, field: CloseOutField.eightySix)
                        AccountField(label: "Who didn't make it", text: $callouts,
                                     focus: $focused, field: CloseOutField.callouts,
                                     showsDivider: false)
                    }

                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    Button {
                        Task {
                            if await viewModel.saveCloseOut(wentWell: wentWell, wentWrong: wentWrong,
                                                            eightySixed: eightySixed, callouts: callouts) {
                                Haptic.success()
                                postedLabel = "Handed off"
                            }
                        }
                    } label: {
                        Text("Hand off the night").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                    .disabled(!canSubmit)
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .accountSheetChrome("Close-out")
            .keyboardNavToolbar($focused)
            .onAppear {
                guard let c = viewModel.closeOut else { return }
                wentWell = c.wentWell ?? ""
                wentWrong = c.wentWrong ?? ""
                eightySixed = c.eightySixed ?? ""
                callouts = c.callouts ?? ""
            }
            .cavnarPostedOverlay(postedLabel) { dismiss() }
        }
    }
}
