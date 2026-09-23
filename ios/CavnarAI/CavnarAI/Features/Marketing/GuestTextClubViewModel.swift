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
    var queuedCount: Int?

    /// Only these can legally be texted, and the gap between this and the full
    /// list is the single most confusing thing about a text club — an owner
    /// with 200 contacts whose campaign reaches 40 needs to see why.
    var textableCount: Int { contacts.filter { $0.status == .textable }.count }

    // Segments, history and the consent picture.
    var segments: [GuestSegment] = []
    var segmentDefaults: [String: String] = [:]
    var selectedSegment = "all"
    /// True when the audience list failed to load. The screen then read
    /// "Goes to 0 guests" while Send still went to "all" — every consented
    /// guest (CLIENT-9). Sending is refused until the audience is known.
    private(set) var audienceUnknown = false
    var campaigns: [GuestCampaign] = []
    var ledger: ConsentLedger?
    var linkURL = ""

    // Newsletter
    var subscriberCount = 0
    /// The subscriber count failed to load — not the same as nobody having
    /// opted in, which is what the screen used to say (CLIENT-58).
    private(set) var newsletterLoadFailed = false
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
        let response: SegmentsResponse
        do {
            response = try await client.send("/mobile/api/guest-segments")
        } catch is CancellationError {
            return
        } catch {
            audienceUnknown = true
            campaignError = "Couldn't load who this would go to, so sending is paused. Tap Retry above."
            return
        }
        if audienceUnknown { campaignError = nil }
        audienceUnknown = false
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
        let response: HistoryResponse
        do {
            response = try await client.send("/mobile/api/guest-campaigns")
        } catch is CancellationError {
            return
        } catch {
            // Not "no campaigns yet" (CLIENT-58) — and never over a more
            // important message already on screen (a send whose outcome is
            // unknown says to check this very history).
            if campaignError == nil {
                campaignError = "Couldn't load past campaigns, so this can't show what already went out."
            }
            return
        }
        campaigns = response.campaigns
        ledger = response.ledger
    }

    private struct NewsletterStatus: Decodable {
        let ok: Bool
        let subscribers: Int
    }

    func loadNewsletter() async {
        do {
            let response: NewsletterStatus = try await client.send("/mobile/api/guest-newsletter")
            subscriberCount = response.subscribers
            newsletterLoadFailed = false
            newsletterError = nil
        } catch is CancellationError {
            return
        } catch {
            newsletterLoadFailed = true
            newsletterError = (error as? APIClient.APIError)?.message ?? "Couldn't load your email list."
        }
    }

    private struct NewsletterBody: Encodable {
        let body: String
        let subject: String?
    }

    private struct NewsletterResponse: Decodable {
        let ok: Bool
        let sent: Int?
        let total: Int?
        /// Still to go out: the server sends the first batch now and the
        /// rest from its scheduler over the next few minutes.
        let queued: Int?
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
                if let queued = response.queued, queued > 0 {
                    newsletterResult = "Sending to \(response.total ?? 0) — \(response.sent ?? 0) out so far, the rest over the next few minutes"
                } else {
                    newsletterResult = "Sent to \(response.sent ?? 0) of \(response.total ?? 0)"
                }
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
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
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

    private typealias OKErrorResponse = APIClient.OKResponse

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

    /// Removed from the list only once the server has deleted it. A failed
    /// DELETE used to be swallowed and the row removed anyway, so the guest
    /// looked gone while still on file (CLIENT-34).
    func deleteContact(_ contact: GuestContact) async {
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/guest-contacts/\(contact.id)", method: .delete
            )
            guard response.ok else {
                errorMessage = response.error ?? "Couldn't delete that contact."
                return
            }
            errorMessage = nil
            contacts.removeAll { $0.id == contact.id }
        } catch is CancellationError {
            return
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't delete that contact."
        }
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
        let queued: Bool?
        let error: String?
    }

    func sendCampaign() async {
        // A second tap while the first send is in flight must not start a
        // second blast (CLIENT-1).
        guard !isSending else { return }
        guard !audienceUnknown else {
            campaignError = "Couldn't load who this would go to, so nothing was sent. Tap Retry above first."
            return
        }
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
            // The server now texts in the background and answers at once
            // with how many it is sending to.
            queuedCount = response.queued == true ? response.total : nil
            if response.ok {
                Haptic.success()
                didSend = true
                await loadHistory()
                await load()
            } else {
                campaignError = response.error ?? "Couldn't send the campaign."
            }
        } catch let error as APIClient.APIError where error.isRetryable || error.status == nil {
            // The answer was lost, not necessarily the send: the server may
            // still be texting. "Tap to retry" invited a second blast. The
            // server skips anyone already texted, but the owner should look
            // at the history first.
            campaignError = "Lost the connection mid-send. Check the campaign history before sending again — anyone already texted is skipped."
            await loadHistory()
        } catch let error as APIClient.APIError {
            campaignError = error.message
        } catch {
            campaignError = "Lost the connection mid-send. Check the campaign history before sending again — anyone already texted is skipped."
            await loadHistory()
        }
    }
}
