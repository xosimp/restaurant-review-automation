import SwiftUI

// The staff app's Agreements (schedule audit 10/10/26 MARKET-2/3/4 — the
// staff portal's agreementsCard, now on the phone): each consent a manager
// asked for, in its exact words, with "I agree" / "No"; a standing
// short-turnaround agreement where the rest rule allows one (paid its
// premium each time) and "Withdraw my agreement"; the good-faith schedule
// estimate with "I've read my estimate" / "Ask for a change"; and the
// person's requests with their written answers. Plus the group's other
// locations (MARKET-16): the person's own yes to being offered shifts
// there, and the ones offered. All from /staff/api/compliance and
// /staff/api/travel; nothing here shows when there is nothing to say.

// MARK: - Payloads

/// One written consent: what it says, and whether it is still waiting.
struct StaffConsent: Decodable, Identifiable, Equatable {
    var id: Int
    var kind: String
    var text: String
    var status: String
    var subjectDate: String? = nil
    var answeredAt: String? = nil

    enum CodingKeys: String, CodingKey {
        case id, kind, text, status
        case subjectDate = "subject_date"
        case answeredAt = "answered_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let i = c.sfInt(.id), let t = c.sfText(.text) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "A consent with no words")
        }
        id = i; text = t
        kind = c.sfText(.kind) ?? ""
        status = c.sfText(.status) ?? "pending"
        subjectDate = c.sfText(.subjectDate)
        answeredAt = c.sfText(.answeredAt)
    }
}

/// The good-faith estimate (fair_workweek): its text and whether it was
/// signed for.
struct StaffEstimate: Decodable, Equatable {
    var id: Int
    var text: String
    var acknowledgedAt: String? = nil
    var issuedAt: String? = nil

    enum CodingKeys: String, CodingKey {
        case id, text
        case acknowledgedAt = "acknowledged_at"
        case issuedAt = "issued_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let i = c.sfInt(.id), let t = c.sfText(.text) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "An estimate with no text")
        }
        id = i; text = t
        acknowledgedAt = c.sfText(.acknowledgedAt)
        issuedAt = c.sfText(.issuedAt)
    }
}

/// A request the person made (a change to the estimate) and its written
/// answer, with when it is due.
struct StaffFWWRequest: Decodable, Identifiable, Equatable {
    var id: Int
    var body: String
    var status: String
    var answer: String? = nil
    var dueAt: String? = nil
    var overdue = false

    enum CodingKeys: String, CodingKey {
        case id, body, status, answer, overdue
        case dueAt = "due_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let i = c.sfInt(.id) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "A request with no id")
        }
        id = i
        body = c.sfText(.body) ?? ""
        status = c.sfText(.status) ?? "open"
        answer = c.sfText(.answer)
        dueAt = c.sfText(.dueAt)
        overdue = c.sfBool(.overdue) ?? false
    }

    /// "Waiting for an answer, due 10/14/26" / "Granted: …".
    var line: String {
        if status == "open" {
            return "Waiting for a written answer" + (dueAt.map { ", due \(CavnarDate.mdy($0))" } ?? "")
                + (overdue ? " (overdue)" : "") + "."
        }
        return status.capitalized + (answer.map { ": \($0)" } ?? ".")
    }
}

/// GET /staff/api/compliance.
struct StaffCompliance: Decodable, Equatable {
    var pending: [StaffConsent] = []
    var standingRest: StaffConsent? = nil
    /// The rest rule's offer of a standing agreement (consent_premium), in
    /// the restaurant's own words. Nil where the rule takes none.
    var restText: String? = nil
    var estimate: StaffEstimate? = nil
    var requests: [StaffFWWRequest] = []

    enum CodingKeys: String, CodingKey { case ok, consents, rest, estimate, requests }
    enum ConsentKeys: String, CodingKey {
        case pending
        case standingRest = "standing_rest"
    }
    enum RestKeys: String, CodingKey { case text }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let k = try? c.nestedContainer(keyedBy: ConsentKeys.self, forKey: .consents) {
            pending = k.setupList(StaffConsent.self, .pending)
            standingRest = (try? k.decodeIfPresent(StaffConsent.self, forKey: .standingRest)) ?? nil
        }
        restText = (try? c.nestedContainer(keyedBy: RestKeys.self, forKey: .rest))?.sfText(.text)
        estimate = (try? c.decodeIfPresent(StaffEstimate.self, forKey: .estimate)) ?? nil
        requests = c.setupList(StaffFWWRequest.self, .requests)
    }

    var isEmpty: Bool {
        pending.isEmpty && standingRest == nil && restText == nil && estimate == nil && requests.isEmpty
    }
}

/// GET /staff/api/travel.
struct StaffTravel: Decodable, Equatable {
    struct Offer: Decodable, Identifiable, Equatable {
        var id: Int
        var place: String
        var dateLabel: String
        var shiftStart: String
        var shiftEnd: String
        var role: String?

        enum CodingKeys: String, CodingKey {
            case id, place, role, date
            case dateLabel = "date_label"
            case shiftStart = "shift_start"
            case shiftEnd = "shift_end"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let i = c.sfInt(.id) else {
                throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "An offer with no id")
            }
            id = i
            place = c.sfText(.place) ?? "another location"
            dateLabel = c.sfText(.dateLabel) ?? c.sfText(.date).map(CavnarDate.mdy) ?? ""
            shiftStart = c.sfText(.shiftStart) ?? ""
            shiftEnd = c.sfText(.shiftEnd) ?? ""
            role = c.sfText(.role)
        }

        /// "Oak St · Sat 10/17/26 · 4:00pm–10:00pm · Server"
        var line: String {
            [place, dateLabel, "\(shiftStart)\u{2013}\(shiftEnd)", role ?? ""].filter { !$0.isEmpty }
                .joined(separator: " \u{00B7} ")
        }
    }

    var travels = false
    var group = false
    var offers: [Offer] = []

    enum CodingKeys: String, CodingKey { case travels, group, offers }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        travels = c.sfBool(.travels) ?? false
        group = c.sfBool(.group) ?? false
        offers = c.setupList(Offer.self, .offers)
    }
}

// MARK: - The short-turnaround ask (409 needs_consent)

/// A claim or an offer's yes that is legal only with the person's written
/// consent to a short turnaround (the rest rule allows it, with a premium):
/// the 409's `needs_consent` — {kind, date, text, gap_hours, need_hours,
/// premium}. The same tap is sent again with `consent_rest: true`.
struct StaffNeedsConsent: Decodable, Equatable {
    var text: String
    var premium: String? = nil

    enum CodingKeys: String, CodingKey { case needsConsent = "needs_consent", error }
    enum InnerKeys: String, CodingKey { case text, premium }

    init(text: String, premium: String? = nil) { self.text = text; self.premium = premium }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let inner = try c.nestedContainer(keyedBy: InnerKeys.self, forKey: .needsConsent)
        guard let t = inner.sfText(.text) ?? c.sfText(.error) else {
            throw DecodingError.dataCorruptedError(forKey: .needsConsent, in: c, debugDescription: "No words to agree to")
        }
        text = t
        premium = inner.sfText(.premium)
    }

    /// The 409 a claim or offer answered with, when it is this ask.
    static func from(_ error: Error) -> StaffNeedsConsent? {
        guard let e = error as? APIClient.APIError, e.status == 409 else { return nil }
        return e.decodeBody(StaffNeedsConsent.self)
    }
}

/// A yes or a claim sent again with the person's written consent.
struct StaffConsentedBody: Encodable, Equatable {
    var accept: Bool? = nil
    let consentRest = true

    enum CodingKeys: String, CodingKey {
        case accept
        case consentRest = "consent_rest"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(accept, forKey: .accept)
        try c.encode(consentRest, forKey: .consentRest)
    }
}

// MARK: - The section

struct StaffAgreementsSection: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var compliance: StaffCompliance?
    @State private var travel: StaffTravel?
    @State private var busy: String?
    @State private var errors: [String: String] = [:]
    @State private var asking = false
    @State private var changeText = ""
    @State private var posted: String?

    private struct AgreeBody: Encodable { let agree: Bool }
    private struct ChangeBody: Encodable {
        let body: String
        var kind = "estimate_change"
    }
    private struct TravelBody: Encodable { let travels: Bool }

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            if let c = compliance, !c.isEmpty { agreements(c) }
            if let t = travel, t.group { travelSection(t) }
        }
        .task { await reload() }
    }

    // MARK: Agreements

    @ViewBuilder
    private func agreements(_ c: StaffCompliance) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Agreements")
            if let posted { StaffUI.note(posted, color: .cavnarGreen) }
            ForEach(c.pending) { consent in
                card {
                    CavnarMixedText(consent.text, role: .body, color: .cavnarInk)
                    if let d = consent.subjectDate { StaffUI.note("For \(CavnarDate.mdy(d))") }
                    HStack(spacing: CavnarSpace.s) {
                        Button {
                            Task { await post("c\(consent.id)", "/staff/api/consents/\(consent.id)/answer",
                                              AgreeBody(agree: true), posted: "Agreed in writing") }
                        } label: { Text("I agree").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy != nil))
                            .disabled(busy != nil)
                        Button {
                            Task { await post("c\(consent.id)", "/staff/api/consents/\(consent.id)/answer",
                                              AgreeBody(agree: false), posted: "Your manager is told it\u{2019}s a no") }
                        } label: { Text("No").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                            .disabled(busy != nil)
                    }
                    if let e = errors["c\(consent.id)"] { StaffUI.errorLine(e) }
                }
            }
            if let standing = c.standingRest {
                card {
                    CavnarMixedText(standing.text, role: .body, color: .cavnarInk)
                    StaffTextButton(title: "Withdraw my agreement", busy: busy == "standing") {
                        Task { await post("standing", "/staff/api/consents/\(standing.id)/revoke", StaffEmptyBody(),
                                          posted: "Withdrawn \u{2014} you won\u{2019}t be given short turnarounds") }
                    }
                    if let e = errors["standing"] { StaffUI.errorLine(e) }
                }
            } else if let rest = c.restText {
                card {
                    CavnarMixedText(rest, role: .body, color: .cavnarInk)
                    StaffTextButton(title: "Agree to short turnarounds", busy: busy == "rest") {
                        Task { await post("rest", "/staff/api/consents/rest-standing", AgreeBody(agree: true),
                                          posted: "Agreed \u{2014} you can withdraw it any time") }
                    }
                    if let e = errors["rest"] { StaffUI.errorLine(e) }
                }
            }
            if let est = c.estimate { estimateCard(est) }
            ForEach(c.requests) { r in
                card {
                    CavnarMixedText(r.body, role: .body, color: .cavnarInk)
                    StaffUI.note(r.line, color: r.overdue ? .cavnarRedText : .cavnarInk2)
                }
            }
        }
    }

    private func estimateCard(_ est: StaffEstimate) -> some View {
        card {
            Text("Your schedule estimate").cavnarText(.label)
            CavnarMixedText(est.text, role: .body)
            if let signed = est.acknowledgedAt {
                StaffUI.note("You read it on \(CavnarDate.mdy(signed)).")
            } else {
                Button {
                    Task { await post("est", "/staff/api/estimate/\(est.id)/acknowledge", StaffEmptyBody(),
                                      posted: "Signed for \u{2014} your manager can see you read it") }
                } label: { Text("I\u{2019}ve read my estimate").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy != nil))
                    .disabled(busy != nil)
            }
            if asking {
                TextField("What would you change?", text: $changeText, axis: .vertical)
                    .font(.cavnar(.body))
                    .lineLimit(2...5)
                    .padding(CavnarSpace.s)
                    .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                        .fill(Color.cavnarPaper2))
                Button {
                    let words = changeText.trimmingCharacters(in: .whitespacesAndNewlines)
                    Task {
                        if await post("change", "/staff/api/estimate/request-change", ChangeBody(body: words),
                                      posted: "Sent \u{2014} your manager answers in writing") {
                            asking = false
                            changeText = ""
                        }
                    }
                } label: { Text("Send my request").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(busy != nil || changeText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            } else {
                StaffTextButton(title: "Ask for a change") { asking = true }
            }
            if let e = errors["est"] ?? errors["change"] { StaffUI.errorLine(e) }
        }
    }

    // MARK: Other locations

    private func travelSection(_ t: StaffTravel) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            AccountSection(kicker: "Our other locations") {
                AccountSwitchRow(label: "Offer me shifts there",
                                 detail: "Only shifts in a role you work, checked against your hours here. Off: nobody there is shown your name.",
                                 isOn: Binding(get: { travel?.travels ?? false }, set: { on in
                                     travel?.travels = on
                                     Task { await setTravel(on) }
                                 }),
                                 busy: busy == "travel", showsDivider: false)
            }
            if let e = errors["travel"] { StaffUI.errorLine(e) }
            ForEach(t.offers) { offer in
                card {
                    CavnarMixedText(offer.line, role: .body, color: .cavnarInk)
                    HStack(spacing: CavnarSpace.s) {
                        Button {
                            Task { await post("o\(offer.id)", "/staff/api/travel/offers/\(offer.id)/respond",
                                              StaffAcceptBody(accept: true), posted: "It\u{2019}s on your schedule there") }
                        } label: { Text("Take it").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy != nil))
                            .disabled(busy != nil)
                        Button {
                            Task { await post("o\(offer.id)", "/staff/api/travel/offers/\(offer.id)/respond",
                                              StaffAcceptBody(accept: false), posted: "Declined \u{2014} they\u{2019}re told") }
                        } label: { Text("No thanks").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                            .disabled(busy != nil)
                    }
                    if let e = errors["o\(offer.id)"] { StaffUI.errorLine(e) }
                }
            }
        }
    }

    // MARK: Plumbing

    private func card<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) { content() }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
    }

    func reload() async {
        compliance = try? await staff.authed("/staff/api/compliance")
        travel = try? await staff.authed("/staff/api/travel")
    }

    @discardableResult
    private func post(_ key: String, _ path: String, _ body: any Encodable, posted: String) async -> Bool {
        busy = key
        errors[key] = nil
        defer { busy = nil }
        do {
            let r: StaffOKResponse = try await staff.authed(path, method: .post, body: body)
            if r.ok {
                Haptic.success()
                self.posted = posted
                await reload()
                return true
            }
            errors[key] = r.error ?? "That didn\u{2019}t go through."
        } catch {
            errors[key] = StaffErrorText.message(error)
        }
        Haptic.error()
        return false
    }

    private func setTravel(_ on: Bool) async {
        busy = "travel"
        errors["travel"] = nil
        defer { busy = nil }
        do {
            let r: StaffOKResponse = try await staff.authed("/staff/api/travel", method: .post, body: TravelBody(travels: on))
            if !r.ok {
                travel?.travels = !on
                errors["travel"] = r.error ?? "That didn\u{2019}t save."
            }
        } catch {
            travel?.travels = !on
            errors["travel"] = StaffErrorText.message(error)
        }
    }
}
