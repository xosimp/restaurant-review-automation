import CoreImage
import CoreImage.CIFilterBuiltins
import Foundation
import Observation
import UIKit

struct GuestContact: Codable, Identifiable {
    let id: Int
    let name: String?
    let phone: String
    let consent: Bool?
    /// The app never decoded this, so an unsubscribed guest looked identical
    /// to one who had simply never opted in — and a guest who HAD consented
    /// and then opted out still showed the green "Consented" badge.
    let unsubscribed: Bool?
    let consentAt: String?
    let lastVisit: String?

    enum CodingKeys: String, CodingKey {
        case id, name, phone, consent, unsubscribed
        case consentAt = "consent_at"
        case lastVisit = "last_visit"
    }

    /// The three states the web contact list has always distinguished. Only
    /// `.textable` may legally receive a campaign (guest_marketing enforces
    /// it server-side); showing the difference here is what stops an owner
    /// wondering why their list of 200 reached 40.
    enum Status { case textable, unsubscribed, noConsent }

    var status: Status {
        if unsubscribed == true { return .unsubscribed }
        return consent == true ? .textable : .noConsent
    }

    var statusLabel: String {
        switch status {
        case .textable: return "Text-eligible"
        case .unsubscribed: return "Unsubscribed"
        case .noConsent: return "No consent yet"
        }
    }
}

@Observable
@MainActor
final class GuestTextClubViewModel {
    var contacts: [GuestContact] = []
    var isLoading = false
    var errorMessage: String?

    var joinURL: String?
    var receiptHint: String?

    // Campaign. These four are guest_marketing.CAMPAIGN_PROMPTS — the app
    // used to offer general/promo/event, and "promo" matched nothing, so it
    // silently fell through to the generic prompt while win-back and loyalty,
    // the two with actual lifecycle intent, were unreachable from the phone.
    static let campaignTypes = ["win_back", "event", "loyalty", "general"]
    var campaignType = "win_back"
    var campaignTopic = ""
    var draftMessage = ""
    var isDrafting = false
    var isSending = false
    var campaignError: String?
    var didSend = false
    var sentCount: Int?

    /// Only these can legally be texted, and the gap between this and the full
    /// list is the single most confusing thing about a text club — an owner
    /// with 200 contacts whose campaign reaches 40 needs to see why.
    var textableCount: Int { contacts.filter { $0.status == .textable }.count }

    // Segments, history and the consent picture.
    var segments: [GuestSegment] = []
    var segmentDefaults: [String: String] = [:]
    var selectedSegment = "all"
    var campaigns: [GuestCampaign] = []
    var ledger: ConsentLedger?
    var linkURL = ""

    // Newsletter
    var subscriberCount = 0
    var newsletterBody = ""
    var newsletterSubject = ""
    var isSendingNewsletter = false
    var newsletterResult: String?
    var newsletterError: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// How many this campaign would actually reach, which is the number an
    /// owner wants before pressing send, not after.
    var selectedSegmentCount: Int {
        segments.first { $0.key == selectedSegment }?.count ?? 0
    }

    var selectedSegmentHelp: String? {
        segments.first { $0.key == selectedSegment }?.help
    }

    private struct SegmentsResponse: Decodable {
        let ok: Bool
        let segments: [GuestSegment]
        let defaults: [String: String]
    }

    func loadSegments() async {
        guard let response: SegmentsResponse = try? await client.send("/mobile/api/guest-segments") else { return }
        segments = response.segments
        segmentDefaults = response.defaults
        // Picking a tone suggests the audience it was written for, instead of
        // leaving the two unrelated the way "win-back to everyone" was.
        if let suggested = response.defaults[campaignType] { selectedSegment = suggested }
    }

    func campaignTypeChanged() {
        if let suggested = segmentDefaults[campaignType] { selectedSegment = suggested }
    }

    private struct HistoryResponse: Decodable {
        let ok: Bool
        let campaigns: [GuestCampaign]
        let ledger: ConsentLedger
    }

    func loadHistory() async {
        guard let response: HistoryResponse = try? await client.send("/mobile/api/guest-campaigns") else { return }
        campaigns = response.campaigns
        ledger = response.ledger
    }

    private struct NewsletterStatus: Decodable {
        let ok: Bool
        let subscribers: Int
    }

    func loadNewsletter() async {
        let response: NewsletterStatus? = try? await client.send("/mobile/api/guest-newsletter")
        subscriberCount = response?.subscribers ?? 0
    }

    private struct NewsletterBody: Encodable {
        let body: String
        let subject: String?
    }

    private struct NewsletterResponse: Decodable {
        let ok: Bool
        let sent: Int?
        let total: Int?
        let subject: String?
        let error: String?
    }

    func sendNewsletter() async {
        isSendingNewsletter = true
        newsletterError = nil
        newsletterResult = nil
        defer { isSendingNewsletter = false }
        do {
            let response: NewsletterResponse = try await client.send(
                "/mobile/api/guest-newsletter", method: .post,
                body: NewsletterBody(body: newsletterBody,
                                     subject: newsletterSubject.isEmpty ? nil : newsletterSubject))
            if response.ok {
                Haptic.success()
                newsletterResult = "Sent to \(response.sent ?? 0) of \(response.total ?? 0)"
                newsletterBody = ""
            } else {
                newsletterError = response.error ?? "Couldn't send that newsletter."
            }
        } catch let error as APIClient.APIError {
            newsletterError = error.message
        } catch {
            newsletterError = "Couldn't send that newsletter."
        }
    }

    private struct ContactsResponse: Decodable {
        let ok: Bool
        let contacts: [GuestContact]
        let error: String?
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: ContactsResponse = try await client.send("/mobile/api/guest-contacts")
            if response.ok {
                contacts = response.contacts
            } else {
                errorMessage = response.error
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load guest contacts."
        }
    }

    private struct JoinLinkResponse: Decodable {
        let ok: Bool
        let joinUrl: String?
        let receiptHint: String?

        enum CodingKeys: String, CodingKey {
            case ok
            case joinUrl = "join_url"
            case receiptHint = "receipt_hint"
        }
    }

    func loadJoinLink() async {
        guard let response: JoinLinkResponse = try? await client.send("/mobile/api/guest-join-link") else { return }
        joinURL = response.joinUrl
        receiptHint = response.receiptHint
    }

    /// The join link as a scannable code, rendered on device — the same
    /// artifact the web tab offers as a PNG download. A QR code on a screen
    /// helps nobody; the point is that it leaves this screen and ends up on a
    /// table tent, so it is generated at a size worth printing and handed to
    /// the share sheet.
    func joinQRCode() -> UIImage? {
        guard let joinURL, let data = joinURL.data(using: .ascii),
              let filter = CIFilter(name: "CIQRCodeGenerator") else { return nil }
        filter.setValue(data, forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 20, y: 20))
        let context = CIContext()
        guard let cgImage = context.createCGImage(scaled, from: scaled.extent) else { return nil }
        return UIImage(cgImage: cgImage)
    }

    /// Starts the automated post-visit review-request countdown
    /// (guest_marketing.run_review_request_followups). The route existed;
    /// the app had no way to call it, so a guest with no natural scan moment
    /// never triggered one.
    func markVisit(_ contact: GuestContact) async {
        _ = try? await client.send(
            "/mobile/api/guest-contacts/\(contact.id)/mark-visit", method: .post
        ) as OKErrorResponse
        Haptic.success()
        await load()
    }

    private struct AddContactBody: Encodable {
        let name: String
        let phone: String
    }

    private struct OKErrorResponse: Decodable {
        let ok: Bool
        let error: String?
    }

    func addContact(name: String, phone: String) async -> Bool {
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/guest-contacts", method: .post, body: AddContactBody(name: name, phone: phone)
            )
            if response.ok {
                await load()
                return true
            }
            errorMessage = response.error
            return false
        } catch {
            errorMessage = "Couldn't add that contact."
            return false
        }
    }

    func deleteContact(_ contact: GuestContact) async {
        _ = try? await client.send(
            "/mobile/api/guest-contacts/\(contact.id)", method: .delete
        ) as APIClient.EmptyResponse
        contacts.removeAll { $0.id == contact.id }
    }

    private struct DraftBody: Encodable {
        let type: String
        let topic: String
    }

    private struct DraftResponse: Decodable {
        let ok: Bool
        let message: String?
        let error: String?
    }

    func draftCampaign() async {
        isDrafting = true
        campaignError = nil
        defer { isDrafting = false }
        do {
            let response: DraftResponse = try await client.send(
                "/mobile/api/guest-campaign/draft", method: .post,
                body: DraftBody(type: campaignType, topic: campaignTopic)
            )
            if response.ok, let message = response.message {
                draftMessage = message
            } else {
                campaignError = response.error ?? "Couldn't draft a message."
            }
        } catch let error as APIClient.APIError {
            campaignError = error.message
        } catch {
            campaignError = "Couldn't draft a message."
        }
    }

    private struct SendBody: Encodable {
        let message: String
        let segment: String
        let linkUrl: String?

        enum CodingKeys: String, CodingKey {
            case message, segment
            case linkUrl = "link_url"
        }
    }

    private struct SendResponse: Decodable {
        let ok: Bool
        let sent: Int?
        let total: Int?
        let error: String?
    }

    func sendCampaign() async {
        isSending = true
        campaignError = nil
        defer { isSending = false }
        do {
            let response: SendResponse = try await client.send(
                "/mobile/api/guest-campaign/send", method: .post,
                body: SendBody(message: draftMessage, segment: selectedSegment,
                               linkUrl: linkURL.isEmpty ? nil : linkURL)
            )
            sentCount = response.sent
            if response.ok {
                Haptic.success()
                didSend = true
                await loadHistory()
                await load()
            } else {
                campaignError = response.error ?? "Couldn't send the campaign."
            }
        } catch let error as APIClient.APIError {
            campaignError = error.message
        } catch {
            campaignError = "Couldn't send the campaign."
        }
    }
}
