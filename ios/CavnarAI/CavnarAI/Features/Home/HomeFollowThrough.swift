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

    /// Every block is optional: a login without an endpoint's permission, or
    /// a module it can't see, simply shows fewer rows rather than an error.
    func load() async {
        isLoading = actions.isEmpty && goals.isEmpty && results.isEmpty
        defer { isLoading = false }
        async let a: ActionsResponse? = try? client.send("/mobile/api/actions", hapticOnError: false)
        async let g: GoalsResponse? = try? client.send("/mobile/api/goals", hapticOnError: false)
        async let o: OutcomesResponse? = try? client.send("/mobile/api/outcomes", hapticOnError: false)
        async let c: CloseOutResponse? = try? client.send("/mobile/api/closeout", hapticOnError: false)
        actions = (await a)?.items ?? []
        goals = (await g)?.goals ?? []
        let outcomes = await o
        results = (outcomes?.outcomes ?? []).filter { $0.summary?.isEmpty == false }
        caveat = outcomes?.caveat
        let close = await c
        closeOut = close?.closeout
        closeOutDate = close?.businessDate
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

            closeOutCard
        }
        .task { await viewModel.load() }
        .sheet(isPresented: $showingCloseOut) {
            CloseOutSheet(viewModel: viewModel)
        }
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
