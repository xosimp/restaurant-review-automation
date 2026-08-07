import Foundation
import Observation

@Observable
@MainActor
final class AccountViewModel {
    var summary: AccountSummary?
    var isLoading = false
    var errorMessage: String?

    var sessions: [AccountSession] = []
    var billing: BillingSummary?

    // Change password
    var isChangingPassword = false
    var changePasswordError: String?
    var changePasswordSucceeded = false

    // 2FA enable flow (send test code -> verify)
    var is2FABusy = false
    var twoFAError: String?
    var twoFATestMasked: String?

    // Alert settings save
    var isSavingAlerts = false
    var saveAlertsError: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            summary = try await client.send("/mobile/api/account")
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load account settings."
        }
        await loadSessions()
    }

    private struct SessionsResponse: Decodable { let ok: Bool; let sessions: [AccountSession] }

    func loadSessions() async {
        do {
            let response: SessionsResponse = try await client.send("/mobile/api/account/sessions")
            sessions = response.sessions
        } catch {
            // Non-fatal — the rest of the Account screen still works without this.
        }
    }

    func loadBilling() async {
        do {
            billing = try await client.send("/mobile/api/account/billing")
        } catch {
            billing = nil
        }
    }

    func revokeOtherSessions() async -> Bool {
        do {
            _ = try await client.send("/mobile/api/sessions/revoke-others", method: .post) as APIClient.EmptyResponse
            await loadSessions()
            return true
        } catch {
            return false
        }
    }

    private struct ChangePasswordBody: Encodable {
        let current: String
        let newPassword: String
        enum CodingKeys: String, CodingKey { case current; case newPassword = "new_password" }
    }

    private struct OKErrorResponse: Decodable {
        let ok: Bool
        let error: String?
    }

    func changePassword(current: String, newPassword: String) async {
        isChangingPassword = true
        changePasswordError = nil
        changePasswordSucceeded = false
        defer { isChangingPassword = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/account/change-password", method: .post,
                body: ChangePasswordBody(current: current, newPassword: newPassword)
            )
            if response.ok {
                changePasswordSucceeded = true
            } else {
                changePasswordError = response.error ?? "Couldn't change your password."
            }
        } catch let error as APIClient.APIError {
            changePasswordError = error.message
        } catch {
            changePasswordError = "Couldn't change your password."
        }
    }

    private struct Send2FATestResponse: Decodable {
        let ok: Bool
        let masked: String?
        let error: String?
    }

    func send2FATest() async {
        is2FABusy = true
        twoFAError = nil
        twoFATestMasked = nil
        defer { is2FABusy = false }
        do {
            let response: Send2FATestResponse = try await client.send(
                "/mobile/api/account/2fa/send-test", method: .post
            )
            if response.ok {
                twoFATestMasked = response.masked
            } else {
                twoFAError = response.error ?? "Couldn't send a test code."
            }
        } catch let error as APIClient.APIError {
            twoFAError = error.message
        } catch {
            twoFAError = "Couldn't send a test code."
        }
    }

    private struct VerifyBody: Encodable { let code: String }

    func verify2FA(code: String) async -> Bool {
        is2FABusy = true
        twoFAError = nil
        defer { is2FABusy = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/account/2fa/verify", method: .post, body: VerifyBody(code: code)
            )
            if response.ok {
                await load()
                return true
            }
            twoFAError = response.error ?? "Incorrect code."
            return false
        } catch let error as APIClient.APIError {
            twoFAError = error.message
            return false
        } catch {
            twoFAError = "Couldn't verify that code."
            return false
        }
    }

    func disable2FA() async {
        do {
            _ = try await client.send("/mobile/api/account/2fa/disable", method: .post) as APIClient.EmptyResponse
            await load()
        } catch {
            // Left as-is — user can retry from the toggle.
        }
    }

    private struct ToggleBody: Encodable { let enabled: Bool }

    func toggleLoginNotify(_ enabled: Bool) async {
        do {
            _ = try await client.send(
                "/mobile/api/account/login-notify", method: .post, body: ToggleBody(enabled: enabled)
            ) as APIClient.EmptyResponse
            // The server saved fine — update local state directly rather than
            // a full reload, so the toggle doesn't snap back to its stale
            // pre-tap value while waiting on a round-trip that already
            // succeeded.
            summary?.account.loginNotify = enabled
        } catch {
            await load()  // resync UI state with server if the toggle silently failed
        }
    }

    private struct AlertContactBody: Encodable {
        let name: String
        let phone: String
    }

    private struct AlertSettingsBody: Encodable {
        let alert1star: Bool
        let alert2star: Bool
        let alertHealth: Bool
        let alertNegSpike: Bool
        let alertNegativeTrend: Bool
        let alertNoResponse: Bool
        let alert5star: Bool
        let alertLaborOver: Bool
        let urgentViaSms: Bool
        let smsConsent: Bool
        let urgentViaEmail: Bool
        let digestEnabled: Bool
        let digestDay: String
        let contacts: [AlertContactBody]

        enum CodingKeys: String, CodingKey {
            case alert1star = "alert_1star"
            case alert2star = "alert_2star"
            case alertHealth = "alert_health"
            case alertNegSpike = "alert_neg_spike"
            case alertNegativeTrend = "alert_negative_trend"
            case alertNoResponse = "alert_no_response"
            case alert5star = "alert_5star"
            case alertLaborOver = "alert_labor_over"
            case urgentViaSms = "urgent_via_sms"
            case smsConsent = "sms_consent"
            case urgentViaEmail = "urgent_via_email"
            case digestEnabled = "digest_enabled"
            case digestDay = "digest_day"
            case contacts
        }
    }

    func saveAlertSettings(_ settings: AlertSettings, contacts: [AlertContact]) async {
        isSavingAlerts = true
        saveAlertsError = nil
        defer { isSavingAlerts = false }
        let body = AlertSettingsBody(
            alert1star: settings.alert1star,
            alert2star: settings.alert2star,
            alertHealth: settings.alertHealth,
            alertNegSpike: settings.alertNegSpike,
            alertNegativeTrend: settings.alertNegativeTrend,
            alertNoResponse: settings.alertNoResponse,
            alert5star: settings.alert5star,
            alertLaborOver: settings.alertLaborOver,
            urgentViaSms: settings.urgentViaSms,
            smsConsent: settings.urgentViaSms,
            urgentViaEmail: settings.urgentViaEmail,
            digestEnabled: settings.digestEnabled,
            digestDay: settings.digestDay,
            contacts: contacts.map { AlertContactBody(name: $0.name, phone: $0.phone) }
        )
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/account/alert-settings", method: .post, body: body
            )
            if response.ok {
                await load()
            } else {
                saveAlertsError = response.error ?? "Couldn't save alert settings."
            }
        } catch let error as APIClient.APIError {
            saveAlertsError = error.message
        } catch {
            saveAlertsError = "Couldn't save alert settings."
        }
    }
}
