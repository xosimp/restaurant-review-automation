import SwiftUI

/// Account -> Restaurant -> "Restaurant profile" (Benchmarking audit #7):
/// who Cavnar compares this restaurant with. How it serves, its concept,
/// whether it is bar-led, its ownership and the year it opened. Saving IS
/// the owner's confirmation — the only thing a peer group is ever built
/// from; a type guessed from the name only pre-fills the "we think you're
/// X — is that right?" prompt. Twin of the web Account block; both read and
/// write /…/account(-settings)/restaurant-profile.
struct RestaurantProfileChoice: Decodable, Hashable {
    let value: String
    let label: String
}

struct RestaurantProfilePayload: Decodable {
    struct Suggestion: Decodable {
        let text: String
        let serviceModel: String?
        let concept: String?
        let confidencePct: Int?
        let cues: [String]?
        enum CodingKeys: String, CodingKey {
            case text, concept, cues
            case serviceModel = "service_model"
            case confidencePct = "confidence_pct"
        }
    }
    /// "Is it still right?" for a CONFIRMED profile (re-audit #38): drift in
    /// the restaurant's own figures, a format that contradicts it, or a
    /// yearly re-confirm. Nothing moves until the owner saves.
    struct Review: Decodable {
        let kind: String?
        let text: String
        let serviceModel: String?
        let concept: String?
        let barLed: Bool?
        enum CodingKeys: String, CodingKey {
            case kind, text, concept
            case serviceModel = "service_model"
            case barLed = "bar_led"
        }
    }
    struct Choices: Decodable {
        let serviceModel: [RestaurantProfileChoice]
        let concept: [RestaurantProfileChoice]
        let ownership: [RestaurantProfileChoice]
        enum CodingKeys: String, CodingKey {
            case concept, ownership
            case serviceModel = "service_model"
        }
    }
    struct Profile: Decodable {
        let serviceModel: String?
        let concept: String?
        let barLed: Bool?
        let ownership: String?
        let openedYear: Int?
        let confirmed: Bool
        let confirmedAt: String?
        let suggestion: Suggestion?
        let review: Review?
        let choices: Choices
        enum CodingKeys: String, CodingKey {
            case concept, ownership, confirmed, suggestion, review, choices
            case serviceModel = "service_model"
            case barLed = "bar_led"
            case openedYear = "opened_year"
            case confirmedAt = "confirmed_at"
        }
    }
    struct Target: Decodable {
        let pct: Double?
        let label: String
    }
    struct Targets: Decodable {
        let labor: Target?
        let food: Target?
    }
    struct CostBasis: Decodable {
        let label: String?
    }
    let ok: Bool
    let error: String?
    let profile: Profile?
    let targets: Targets?
    let laborCostBasis: CostBasis?
    enum CodingKeys: String, CodingKey {
        case ok, error, profile, targets
        case laborCostBasis = "labor_cost_basis"
    }
}

private struct RestaurantProfileBody: Encodable {
    let service_model: String
    let concept: String
    let bar_led: Bool
    let ownership: String
    let opened_year: String
}

struct AccountRestaurantProfileSheet: View {
    let canEdit: Bool
    @Environment(\.dismiss) private var dismiss

    @State private var payload: RestaurantProfilePayload?
    @State private var serviceModel = ""
    @State private var concept = ""
    @State private var barLed = false
    @State private var ownership = ""
    @State private var openedYear = ""
    @State private var isSaving = false
    @State private var errorText: String?
    @State private var postedLabel: String?

    private static let path = "/mobile/api/account/restaurant-profile"

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Restaurant profile") {
                        GlowBadge(systemImage: "person.2.crop.square.stack", size: 64)
                    } subtitle: {
                        Text("Who you're compared with")
                    }

                    if let p = payload?.profile {
                        HStack(spacing: 6) {
                            AccountChip(text: Self.confirmedText(p), muted: !p.confirmed)
                        }
                        if let s = p.suggestion, !p.confirmed {
                            suggestionCard(s)
                        } else if let r = p.review, p.confirmed {
                            reviewCard(r)
                        }
                        profileSection(p)
                        targetsSection
                    } else if errorText == nil {
                        CavnarShimmerText(text: "Loading…")
                    }

                    if let errorText {
                        Text(errorText).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    if canEdit, payload?.profile != nil {
                        Button {
                            Task { await save() }
                        } label: {
                            Group {
                                if isSaving { CavnarShimmerText(text: "Saving…") } else { Text("Confirm profile") }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: isSaving || serviceModel.isEmpty))
                        .disabled(isSaving || serviceModel.isEmpty)
                    } else if !canEdit {
                        Text("Only the account owner can change the restaurant profile.")
                            .font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Profile")
            .cavnarPostedOverlay(postedLabel) { dismiss() }
            .task { await load() }
        }
    }

    /// "Confirmed 9/24/26" — the stamp is UTC, shown on the phone's own
    /// calendar day, as the web shows it (Benchmarking #33).
    static func confirmedText(_ p: RestaurantProfilePayload.Profile) -> String {
        guard p.confirmed else { return "Not confirmed" }
        guard let at = p.confirmedAt, !at.isEmpty else { return "Confirmed" }
        return "Confirmed " + CavnarDate.mdyLocal(at)
    }

    // MARK: - Sections

    private func suggestionCard(_ s: RestaurantProfilePayload.Suggestion) -> some View {
        AccountSection(kicker: "Our guess") {
            VStack(alignment: .leading, spacing: 6) {
                Text(s.text).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                Text("A guess from \((s.cues ?? []).isEmpty ? "your name" : (s.cues ?? []).joined(separator: ", ")) — \(s.confidencePct ?? 0)% confident. Until you confirm, no other restaurant is compared with you.")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                if canEdit {
                    HStack(spacing: 10) {
                        Button("Yes, that's right") {
                            if let sm = s.serviceModel { serviceModel = sm }
                            if let c = s.concept { concept = c }
                            if !serviceModel.isEmpty { Task { await save() } }
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: isSaving))
                    }
                    .padding(.top, 4)
                }
            }
            .padding(.vertical, 9)
        }
    }

    private func reviewCard(_ r: RestaurantProfilePayload.Review) -> some View {
        AccountSection(kicker: "Is this still right?") {
            VStack(alignment: .leading, spacing: 6) {
                Text(r.text).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                Text("From your own figures. Who you're compared with doesn't change until you confirm.")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                if canEdit {
                    HStack(spacing: 10) {
                        Button("Yes, update it") {
                            if let sm = r.serviceModel { serviceModel = sm }
                            if let c = r.concept { concept = c }
                            if let b = r.barLed { barLed = b }
                            if !serviceModel.isEmpty { Task { await save() } }
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: isSaving))
                    }
                    .padding(.top, 4)
                }
            }
            .padding(.vertical, 9)
        }
    }

    private func profileSection(_ p: RestaurantProfilePayload.Profile) -> some View {
        AccountSection(kicker: "How you serve") {
            pickerRow("Service", selection: $serviceModel, choices: p.choices.serviceModel, empty: "Not confirmed")
            pickerRow("Concept", selection: $concept, choices: p.choices.concept, empty: "Not set")
            AccountSwitchRow(label: "Bar-led", detail: "Alcohol is about 40% or more of sales.",
                             isOn: $barLed, disabled: !canEdit)
            pickerRow("Ownership", selection: $ownership, choices: p.choices.ownership, empty: "Not set")
            AccountKVRow(label: "Year opened", showsDivider: false) {
                TextField("2019", text: $openedYear)
                    .keyboardType(.numberPad)
                    .multilineTextAlignment(.trailing)
                    .font(.cavnarNumber(15, weight: 600))
                    .frame(width: 80)
                    .disabled(!canEdit)
            }
        }
    }

    private var targetsSection: some View {
        AccountSection(kicker: "Your targets") {
            if let t = payload?.targets?.labor {
                AccountKVRow(label: "Labor") { targetValue(t) }
            }
            if let t = payload?.targets?.food {
                AccountKVRow(label: "Food cost", showsDivider: payload?.laborCostBasis?.label != nil) { targetValue(t) }
            }
            if let basis = payload?.laborCostBasis?.label {
                AccountKVRow(label: "Labor cost from", showsDivider: false) {
                    Text(basis).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk2)
                        .multilineTextAlignment(.trailing)
                }
            }
        }
    }

    private func targetValue(_ t: RestaurantProfilePayload.Target) -> some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(t.pct.map { String(format: "%g%%", $0) } ?? "—")
                .font(.cavnarNumber(15, weight: 600)).foregroundStyle(Color.cavnarInk2)
            Text(t.label).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
        }
    }

    private func pickerRow(_ label: String, selection: Binding<String>, choices: [RestaurantProfileChoice],
                           empty: String) -> some View {
        AccountKVRow(label: label) {
            Picker("", selection: selection) {
                Text(empty).tag("")
                ForEach(choices, id: \.value) { c in
                    Text(c.label).tag(c.value)
                }
            }
            .tint(Color.cavnarEmber)
            .disabled(!canEdit)
        }
    }

    // MARK: - Network

    private func apply(_ d: RestaurantProfilePayload) {
        payload = d
        guard let p = d.profile else { return }
        serviceModel = p.serviceModel ?? ""
        concept = p.concept ?? ""
        barLed = p.barLed ?? false
        ownership = p.ownership ?? ""
        openedYear = p.openedYear.map(String.init) ?? ""
    }

    private func load() async {
        do {
            let d: RestaurantProfilePayload = try await APIClient.shared.send(Self.path, hapticOnError: false)
            if d.ok { apply(d) } else { errorText = d.error ?? "Couldn't load the profile." }
        } catch let error as APIClient.APIError {
            errorText = error.message
        } catch {
            errorText = "Couldn't load the profile."
        }
    }

    private func save() async {
        guard !serviceModel.isEmpty else { return }
        isSaving = true; errorText = nil
        defer { isSaving = false }
        do {
            let body = RestaurantProfileBody(service_model: serviceModel, concept: concept, bar_led: barLed,
                                             ownership: ownership, opened_year: openedYear)
            let d: RestaurantProfilePayload = try await APIClient.shared.send(Self.path, method: .post, body: body)
            if d.ok {
                apply(d)
                Haptic.success()
                postedLabel = "Profile confirmed"
                // The How you compare cards were read under the old profile:
                // re-read them now rather than saying "isn't confirmed" for
                // another five minutes (Benchmarking #33).
                Task { await BenchmarkCardStore.shared.reloadAll() }
            } else {
                errorText = d.error ?? "Couldn't save the profile."
            }
        } catch let error as APIClient.APIError {
            errorText = error.message
        } catch {
            errorText = "Couldn't save the profile."
        }
    }
}
