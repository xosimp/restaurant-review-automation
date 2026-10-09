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

    /// A choice changed and not yet confirmed — Back asks before dropping it.
    private var isDirty: Bool {
        guard canEdit, !isSaving, let p = payload?.profile else { return false }
        return serviceModel != (p.serviceModel ?? "") || concept != (p.concept ?? "")
            || barLed != (p.barLed ?? false) || ownership != (p.ownership ?? "")
            || openedYear != (p.openedYear.map(String.init) ?? "")
    }

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
                        // "Your targets" left this sheet: the targets live
                        // in Account → Restaurant (iOS readability round).
                        profileSection(p)
                    } else if errorText == nil {
                        CavnarSkeletonLines()
                    }

                    if let errorText {
                        Text(errorText).cavnarText(.secondary, color: .cavnarRedText)
                    }

                    // One primary on the sheet (re-audit L13): the card's
                    // "Yes" when there is a guess or a re-check to answer;
                    // otherwise a one-tap confirm of what's on file, only
                    // while it isn't confirmed.
                    if canEdit, let p = payload?.profile, !Self.cardShown(p), !p.confirmed {
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
                            .cavnarText(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Profile", isDirty: isDirty)
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
                Text(s.text).cavnarText(.label)
                // The cues and the "% confident" are the guesser's working,
                // not the owner's question (iOS readability round).
                Text("Until you confirm, no other restaurant is compared with you.")
                    .cavnarText(.secondary)
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
                Text(r.text).cavnarText(.label)
                Text("From your own figures. Who you're compared with doesn't change until you confirm.")
                    .cavnarText(.secondary)
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

    /// A guess or a re-check card is on screen — its "Yes" is the primary.
    static func cardShown(_ p: RestaurantProfilePayload.Profile) -> Bool {
        (p.suggestion != nil && !p.confirmed) || (p.review != nil && p.confirmed)
    }

    /// What's on file, read here; changing it is the web's form (re-audit
    /// L13 — a five-field form on the phone beside a one-tap confirm).
    private func profileSection(_ p: RestaurantProfilePayload.Profile) -> some View {
        AccountSection(kicker: "How you serve") {
            valueRow("Service", Self.label(serviceModel, in: p.choices.serviceModel, empty: "Not confirmed"))
            valueRow("Concept", Self.label(concept, in: p.choices.concept, empty: "Not set"))
            valueRow("Bar-led", barLed ? "Yes" : "No")
            valueRow("Ownership", Self.label(ownership, in: p.choices.ownership, empty: "Not set"))
            AccountKVRow(label: "Year opened") {
                AccountValue(text: openedYear.isEmpty ? "Not set" : openedYear, isNumber: !openedYear.isEmpty)
            }
            CavnarWebLinkRow(title: "How you serve", path: "account/restaurant",
                             actionLabel: canEdit ? "Edit on the web" : "Open on the web")
        }
    }

    private func valueRow(_ label: String, _ value: String) -> some View {
        AccountKVRow(label: label) { AccountValue(text: value) }
    }

    static func label(_ value: String, in choices: [RestaurantProfileChoice], empty: String) -> String {
        guard !value.isEmpty else { return empty }
        return choices.first { $0.value == value }?.label ?? empty
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
