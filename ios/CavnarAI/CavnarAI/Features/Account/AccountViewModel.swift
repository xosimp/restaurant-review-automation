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
            // hapticOnError: false — this is a background enrichment call
            // the user never sees fail (no error message shown either),
            // so buzzing the same "you failed to log in" pattern for it
            // was pure noise, not signal.
            let response: SessionsResponse = try await client.send(
                "/mobile/api/account/sessions", hapticOnError: false
            )
            sessions = response.sessions
        } catch {
            // Non-fatal — the rest of the Account screen still works without this.
        }
    }

    func loadBilling() async {
        do {
            // hapticOnError: false — see loadSessions() above. A restaurant
            // with no active subscription hits this constantly and that's
            // an expected, normal state (the UI just shows "No active
            // subscription"), not a failure worth an error buzz.
            billing = try await client.send("/mobile/api/account/billing", hapticOnError: false)
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
        let alertQuietStart: String?
        let alertQuietEnd: String?
        let al1starPush: Bool
        let al2starPush: Bool
        let al5starPush: Bool
        let alHealthPush: Bool
        let alSpikePush: Bool
        let alUnresPush: Bool
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
            case alertQuietStart = "alert_quiet_start"
            case alertQuietEnd = "alert_quiet_end"
            case al1starPush = "al_1star_push"
            case al2starPush = "al_2star_push"
            case al5starPush = "al_5star_push"
            case alHealthPush = "al_health_push"
            case alSpikePush = "al_spike_push"
            case alUnresPush = "al_unres_push"
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
            alertQuietStart: settings.alertQuietStart,
            alertQuietEnd: settings.alertQuietEnd,
            al1starPush: settings.al1starPush,
            al2starPush: settings.al2starPush,
            al5starPush: settings.al5starPush,
            alHealthPush: settings.alHealthPush,
            alSpikePush: settings.alSpikePush,
            alUnresPush: settings.alUnresPush,
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

    // Update email

    var isUpdatingEmail = false
    var updateEmailError: String?
    var updateEmailSucceeded = false

    private struct UpdateEmailBody: Encodable {
        let newEmail: String
        let currentPassword: String
        enum CodingKeys: String, CodingKey {
            case newEmail = "new_email"
            case currentPassword = "current_password"
        }
    }

    func updateEmail(newEmail: String, currentPassword: String) async {
        isUpdatingEmail = true
        updateEmailError = nil
        updateEmailSucceeded = false
        defer { isUpdatingEmail = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/account/update-email", method: .post,
                body: UpdateEmailBody(newEmail: newEmail, currentPassword: currentPassword)
            )
            if response.ok {
                updateEmailSucceeded = true
                await load()
            } else {
                updateEmailError = response.error ?? "Couldn't update your email."
            }
        } catch let error as APIClient.APIError {
            updateEmailError = error.message
        } catch {
            updateEmailError = "Couldn't update your email."
        }
    }

    // Update profile (owner contact info + AI-voice notes only — the
    // fields client_api.py parses by exact string match, like
    // restaurant name/location/neighborhood/vibe/known-for, stay
    // admin-set and aren't part of this body)

    var isSavingProfile = false
    var saveProfileError: String?
    var saveProfileSucceeded = false

    private struct UpdateProfileBody: Encodable {
        let ownerName: String
        let ownerPhone: String
        let voiceNotes: String
        let neverSay: String
        let menuNotes: String
        enum CodingKeys: String, CodingKey {
            case ownerName = "owner_name"
            case ownerPhone = "owner_phone"
            case voiceNotes = "voice_notes"
            case neverSay = "never_say"
            case menuNotes = "menu_notes"
        }
    }

    func updateProfile(ownerName: String, ownerPhone: String, voiceNotes: String, neverSay: String, menuNotes: String) async {
        isSavingProfile = true
        saveProfileError = nil
        saveProfileSucceeded = false
        defer { isSavingProfile = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/account/update-profile", method: .post,
                body: UpdateProfileBody(
                    ownerName: ownerName, ownerPhone: ownerPhone,
                    voiceNotes: voiceNotes, neverSay: neverSay, menuNotes: menuNotes
                )
            )
            if response.ok {
                saveProfileSucceeded = true
                await load()
            } else {
                saveProfileError = response.error ?? "Couldn't save your profile."
            }
        } catch let error as APIClient.APIError {
            saveProfileError = error.message
        } catch {
            saveProfileError = "Couldn't save your profile."
        }
    }

    // Connections — Google Business

    var isConnectingGoogle = false
    var connectGoogleError: String?

    private struct GoogleAuthorizeResponse: Decodable {
        let ok: Bool
        let url: String?
        let error: String?
    }

    func connectGoogleBusiness() async {
        isConnectingGoogle = true
        connectGoogleError = nil
        defer { isConnectingGoogle = false }
        do {
            let response: GoogleAuthorizeResponse = try await client.send("/mobile/api/connections/google/authorize")
            guard response.ok, let urlString = response.url, let url = URL(string: urlString) else {
                connectGoogleError = response.error ?? "Couldn't start Google connect."
                return
            }
            try await GMBConnectCoordinator().connect(authorizeURL: url)
            await load()
        } catch let error as GMBConnectError {
            switch error {
            case .cancelled: break
            case .server(let msg): connectGoogleError = msg
            }
        } catch let error as APIClient.APIError {
            connectGoogleError = error.message
        } catch {
            connectGoogleError = "Couldn't connect Google Business."
        }
    }

    func disconnectGoogleBusiness() async {
        do {
            let _: APIClient.EmptyResponse = try await client.send("/mobile/api/connections/google", method: .delete)
            await load()
        } catch {
            // Same low-stakes fallback as disconnectToast() below.
        }
    }

    // Connections — Toast

    var isConnectingToast = false
    var connectToastError: String?
    var connectToastSucceeded = false

    private struct ConnectToastBody: Encodable {
        let toastClientId: String
        let toastClientSecret: String
        let toastRestaurantGuid: String
        enum CodingKeys: String, CodingKey {
            case toastClientId = "toast_client_id"
            case toastClientSecret = "toast_client_secret"
            case toastRestaurantGuid = "toast_restaurant_guid"
        }
    }

    func connectToast(clientId: String, clientSecret: String, restaurantGuid: String) async {
        isConnectingToast = true
        connectToastError = nil
        connectToastSucceeded = false
        defer { isConnectingToast = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/connections/toast", method: .post,
                body: ConnectToastBody(toastClientId: clientId, toastClientSecret: clientSecret, toastRestaurantGuid: restaurantGuid)
            )
            if response.ok {
                connectToastSucceeded = true
                await load()
            } else {
                connectToastError = response.error ?? "Couldn't connect Toast."
            }
        } catch let error as APIClient.APIError {
            connectToastError = error.message
        } catch {
            connectToastError = "Couldn't connect Toast."
        }
    }

    func disconnectToast() async {
        do {
            let _: APIClient.EmptyResponse = try await client.send("/mobile/api/connections/toast", method: .delete)
            await load()
        } catch {
            // Low-stakes background action from a status row — a failed
            // disconnect just leaves the existing connected state showing,
            // which is a safe, obvious fallback with no separate UI for it.
        }
    }
}
