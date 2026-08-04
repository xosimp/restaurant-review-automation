import Foundation
import Observation

struct GuestContact: Codable, Identifiable {
    let id: Int
    let name: String?
    let phone: String
    let consent: Bool?
    let lastVisit: String?

    enum CodingKeys: String, CodingKey {
        case id, name, phone, consent
        case lastVisit = "last_visit"
    }
}

@Observable
@MainActor
final class GuestTextClubViewModel {
    var contacts: [GuestContact] = []
    var isLoading = false
    var errorMessage: String?

    var joinURL: String?

    // Campaign
    var campaignType = "general"
    var campaignTopic = ""
    var draftMessage = ""
    var isDrafting = false
    var isSending = false
    var campaignError: String?
    var didSend = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
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

        enum CodingKeys: String, CodingKey {
            case ok
            case joinUrl = "join_url"
        }
    }

    func loadJoinLink() async {
        joinURL = try? await (client.send("/mobile/api/guest-join-link") as JoinLinkResponse).joinUrl
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
    }

    func sendCampaign() async {
        isSending = true
        campaignError = nil
        defer { isSending = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/guest-campaign/send", method: .post, body: SendBody(message: draftMessage)
            )
            if response.ok {
                didSend = true
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
