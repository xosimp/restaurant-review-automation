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

    // Sign-in activity log
    var loginHistory: [LoginHistoryEntry] = []
    var isLoadingLoginHistory = false

    // Export my data
    var isExportingData = false
    var exportDataError: String?
    var exportDataSucceeded = false

    // Self-serve test digest
    var isSendingTestDigest = false
    var testDigestError: String?
    var testDigestSucceeded = false

    // Marketing opt-out
    var isTogglingMarketingOptOut = false

    // 2FA backup codes
    var backupCodesRemaining: Int?
    var isBackupCodesBusy = false
    var backupCodesError: String?

    // Team (invite / manage access)
    var teamMembers: [TeamMember] = []
    var isLoadingTeam = false
    var isInvitingTeamMember = false
    var inviteTeamError: String?
    var revokeTeamError: String?

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
        securityActionError = nil
        do {
            _ = try await client.send("/mobile/api/sessions/revoke-others", method: .post) as APIClient.EmptyResponse
            await loadSessions()
            return true
        } catch let error as APIClient.APIError {
            // Same reasoning as disable2FA: "Sign out all other devices"
            // quietly doing nothing is a security-relevant false belief.
            securityActionError = error.message
            return false
        } catch {
            securityActionError = "Couldn't sign out your other devices — check your connection and try again."
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
                // Refreshes summary.account.passwordStrength/passwordChangedAt
                // so the Security sheet's tile reflects the new password
                // immediately instead of waiting for the sheet to reopen.
                await load()
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
        let method: String?
    }

    private struct Send2FATestBody: Encodable { let method: String }

    // twoFATestMethod records which channel the code actually went out on
    // (echoed back by the server), so the "enter code" screen can show the
    // right copy even if send2FATest is called again later with a stale
    // default.
    var twoFATestMethod: String = "email"

    func send2FATest(method: String) async {
        is2FABusy = true
        twoFAError = nil
        twoFATestMasked = nil
        defer { is2FABusy = false }
        do {
            let response: Send2FATestResponse = try await client.send(
                "/mobile/api/account/2fa/send-test", method: .post, body: Send2FATestBody(method: method)
            )
            if response.ok {
                twoFATestMasked = response.masked
                twoFATestMethod = response.method ?? method
            } else {
                twoFAError = response.error ?? "Couldn't send a test code."
            }
        } catch let error as APIClient.APIError {
            twoFAError = error.message
        } catch {
            twoFAError = "Couldn't send a test code."
        }
    }

    private struct VerifyBody: Encodable { let code: String; let method: String }
    private struct VerifyResponse: Decodable { let ok: Bool; let error: String?; let backupCodes: [String]?
        enum CodingKeys: String, CodingKey { case ok, error; case backupCodes = "backup_codes" }
    }

    // Set the moment 2FA is first enabled — the codes are shown exactly
    // once (only the hash is ever persisted server-side), so the setup
    // sheet reads this right after a successful verify and never again.
    var freshBackupCodes: [String]?

    func verify2FA(code: String) async -> Bool {
        is2FABusy = true
        twoFAError = nil
        defer { is2FABusy = false }
        do {
            let response: VerifyResponse = try await client.send(
                "/mobile/api/account/2fa/verify", method: .post, body: VerifyBody(code: code, method: twoFATestMethod)
            )
            if response.ok {
                freshBackupCodes = response.backupCodes
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

    /// Surfaced next to the Sign-in rows. A security control that silently
    /// no-ops leaves the user believing 2FA is off when it is still on —
    /// strictly worse than a visible error (audit 2.4).
    var securityActionError: String?

    @discardableResult
    func disable2FA() async -> Bool {
        securityActionError = nil
        do {
            _ = try await client.send("/mobile/api/account/2fa/disable", method: .post) as APIClient.EmptyResponse
            await load()
            return true
        } catch let error as APIClient.APIError {
            securityActionError = error.message
            return false
        } catch {
            securityActionError = "Couldn't turn two-factor off — check your connection and try again."
            return false
        }
    }

    private struct ToggleBody: Encodable { let enabled: Bool }

    // Returns whether it actually succeeded — the Security sheet's posted
    // overlay only fires on a real success (see cavnarPostedOverlay's own
    // doc comment), never optimistically, so a caller needs this instead
    // of just firing-and-forgetting.
    @discardableResult
    var isTogglingLoginNotify = false

    func toggleLoginNotify(_ enabled: Bool) async -> Bool {
        isTogglingLoginNotify = true
        defer { isTogglingLoginNotify = false }
        do {
            _ = try await client.send(
                "/mobile/api/account/login-notify", method: .post, body: ToggleBody(enabled: enabled)
            ) as APIClient.EmptyResponse
            // The server saved fine — update local state directly rather than
            // a full reload, so the toggle doesn't snap back to its stale
            // pre-tap value while waiting on a round-trip that already
            // succeeded.
            summary?.account.loginNotify = enabled
            return true
        } catch {
            await load()  // resync UI state with server if the toggle silently failed
            return false
        }
    }

    private struct MarketingOptOutBody: Encodable { let optedOut: Bool
        enum CodingKeys: String, CodingKey { case optedOut = "opted_out" }
    }

    @discardableResult
    func toggleMarketingOptOut(_ optedOut: Bool) async -> Bool {
        isTogglingMarketingOptOut = true
        defer { isTogglingMarketingOptOut = false }
        do {
            _ = try await client.send(
                "/mobile/api/account/marketing-opt-out", method: .post, body: MarketingOptOutBody(optedOut: optedOut)
            ) as APIClient.EmptyResponse
            summary?.account.marketingEmailsOptOut = optedOut
            return true
        } catch {
            await load()
            return false
        }
    }

    private struct HistoryResponse: Decodable { let ok: Bool; let history: [LoginHistoryEntry] }

    func loadLoginHistory() async {
        isLoadingLoginHistory = true
        defer { isLoadingLoginHistory = false }
        do {
            let response: HistoryResponse = try await client.send("/mobile/api/account/login-history", hapticOnError: false)
            loginHistory = response.history
        } catch {
            // Non-fatal — sheet just shows an empty state.
        }
    }

    private struct ExportEmailResponse: Decodable { let ok: Bool; let email: String?; let error: String? }

    private struct ExportBody: Encodable { let scopes: [String] }

    func exportData(scopes: [String] = ["reviews"]) async {
        isExportingData = true
        exportDataError = nil
        exportDataSucceeded = false
        defer { isExportingData = false }
        do {
            let response: ExportEmailResponse = try await client.send("/mobile/api/account/export-data", method: .post, body: ExportBody(scopes: scopes))
            if response.ok {
                exportDataSucceeded = true
            } else {
                exportDataError = response.error ?? "Couldn't export your data."
            }
        } catch let error as APIClient.APIError {
            exportDataError = error.message
        } catch {
            exportDataError = "Couldn't export your data."
        }
    }

    func sendTestDigest() async {
        isSendingTestDigest = true
        testDigestError = nil
        testDigestSucceeded = false
        defer { isSendingTestDigest = false }
        do {
            let response: ExportEmailResponse = try await client.send("/mobile/api/account/send-test-digest", method: .post)
            if response.ok {
                testDigestSucceeded = true
            } else {
                testDigestError = response.error ?? "Couldn't send a preview."
            }
        } catch let error as APIClient.APIError {
            testDigestError = error.message
        } catch {
            testDigestError = "Couldn't send a preview."
        }
    }

    private struct BackupCodesStatusResponse: Decodable { let ok: Bool; let remaining: Int? }
    private struct BackupCodesResponse: Decodable { let ok: Bool; let backupCodes: [String]?
        enum CodingKeys: String, CodingKey { case ok; case backupCodes = "backup_codes" }
    }

    func loadBackupCodesStatus() async {
        do {
            let response: BackupCodesStatusResponse = try await client.send("/mobile/api/account/2fa/backup-codes", hapticOnError: false)
            backupCodesRemaining = response.remaining
        } catch {
            // Non-fatal.
        }
    }

    func regenerateBackupCodes() async -> [String]? {
        isBackupCodesBusy = true
        backupCodesError = nil
        defer { isBackupCodesBusy = false }
        do {
            let response: BackupCodesResponse = try await client.send("/mobile/api/account/2fa/backup-codes", method: .post)
            if response.ok, let codes = response.backupCodes {
                backupCodesRemaining = codes.count
                return codes
            }
            backupCodesError = "Couldn't generate new codes."
            return nil
        } catch let error as APIClient.APIError {
            backupCodesError = error.message
            return nil
        } catch {
            backupCodesError = "Couldn't generate new codes."
            return nil
        }
    }

    private struct TeamResponse: Decodable { let ok: Bool; let members: [TeamMember] }

    func loadTeam() async {
        isLoadingTeam = true
        defer { isLoadingTeam = false }
        do {
            let response: TeamResponse = try await client.send("/mobile/api/account/team", hapticOnError: false)
            teamMembers = response.members
        } catch {
            // Non-fatal — sheet just shows an empty state.
        }
    }

    private struct InviteBody: Encodable { let name: String; let email: String }
    private struct InviteResponse: Decodable { let ok: Bool; let error: String? }

    @discardableResult
    func inviteTeamMember(name: String, email: String) async -> Bool {
        isInvitingTeamMember = true
        inviteTeamError = nil
        defer { isInvitingTeamMember = false }
        do {
            let response: InviteResponse = try await client.send(
                "/mobile/api/account/team/invite", method: .post, body: InviteBody(name: name, email: email)
            )
            if response.ok {
                await loadTeam()
                return true
            }
            inviteTeamError = response.error ?? "Couldn't add that teammate."
            return false
        } catch let error as APIClient.APIError {
            inviteTeamError = error.message
            return false
        } catch {
            inviteTeamError = "Couldn't add that teammate."
            return false
        }
    }

    @discardableResult
    func revokeTeamMember(_ userID: Int) async -> Bool {
        revokeTeamError = nil
        do {
            let response: InviteResponse = try await client.send(
                "/mobile/api/account/team/\(userID)/revoke", method: .post
            )
            if response.ok {
                await loadTeam()
                return true
            }
            revokeTeamError = response.error ?? "Couldn't remove that teammate."
            return false
        } catch let error as APIClient.APIError {
            revokeTeamError = error.message
            return false
        } catch {
            revokeTeamError = "Couldn't remove that teammate."
            return false
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
        let alertHealthBypassQuiet: Bool
        let alertFoodWaste: Bool
        let alertAiVisibilityDrop: Bool
        let alertExtraEmails: String
        let pushSound: Bool
        let contacts: [AlertContactBody]

        enum CodingKeys: String, CodingKey {
            case alertHealthBypassQuiet = "alert_health_bypass_quiet"
            case alertFoodWaste = "alert_food_waste"
            case alertAiVisibilityDrop = "alert_ai_visibility_drop"
            case alertExtraEmails = "alert_extra_emails"
            case pushSound = "push_sound"
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
            alertHealthBypassQuiet: settings.alertHealthBypassQuiet,
            alertFoodWaste: settings.alertFoodWaste,
            alertAiVisibilityDrop: settings.alertAiVisibilityDrop,
            alertExtraEmails: settings.alertExtraEmails,
            pushSound: settings.pushSound,
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
        let timezone: String
        let signOffName: String
        let responseLanguage: String
        let tonePreset: String
        enum CodingKeys: String, CodingKey {
            case signOffName = "sign_off_name"
            case responseLanguage = "response_language"
            case tonePreset = "tone_preset"
            case ownerName = "owner_name"
            case ownerPhone = "owner_phone"
            case voiceNotes = "voice_notes"
            case neverSay = "never_say"
            case menuNotes = "menu_notes"
            case timezone
        }
    }

    func updateProfile(ownerName: String, ownerPhone: String, voiceNotes: String, neverSay: String, menuNotes: String, timezone: String,
                       signOffName: String = "", responseLanguage: String = "", tonePreset: String = "") async {
        isSavingProfile = true
        saveProfileError = nil
        saveProfileSucceeded = false
        defer { isSavingProfile = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/account/update-profile", method: .post,
                body: UpdateProfileBody(
                    ownerName: ownerName, ownerPhone: ownerPhone,
                    voiceNotes: voiceNotes, neverSay: neverSay, menuNotes: menuNotes, timezone: timezone,
                    signOffName: signOffName, responseLanguage: responseLanguage, tonePreset: tonePreset
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

    // MARK: - Settings audit additions

    private struct ActivityResponse: Decodable { let ok: Bool; let events: [AccountActivityEvent] }
    var activity: [AccountActivityEvent] = []
    var isLoadingActivity = false

    func loadActivity() async {
        isLoadingActivity = true
        defer { isLoadingActivity = false }
        do {
            let response: ActivityResponse = try await client.send("/mobile/api/account/activity", hapticOnError: false)
            activity = response.events
        } catch {}
    }

    private struct TrustedDevicesResponse: Decodable { let ok: Bool; let devices: [TrustedDevice] }
    var trustedDevices: [TrustedDevice] = []
    var isLoadingTrustedDevices = false
    var trustedDevicesError: String?

    func loadTrustedDevices() async {
        isLoadingTrustedDevices = true
        defer { isLoadingTrustedDevices = false }
        do {
            let response: TrustedDevicesResponse = try await client.send("/mobile/api/account/2fa/trusted-devices", hapticOnError: false)
            trustedDevices = response.devices
        } catch {}
    }

    func revokeTrustedDevice(_ id: Int) async -> Bool {
        trustedDevicesError = nil
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/2fa/trusted-devices/\(id)/revoke", method: .post)
            if response.ok { await loadTrustedDevices(); return true }
            trustedDevicesError = response.error ?? "Couldn't forget that device."
        } catch let error as APIClient.APIError {
            trustedDevicesError = error.message
        } catch {
            trustedDevicesError = "Couldn't forget that device."
        }
        return false
    }

    func revokeAllTrustedDevices() async -> Bool {
        trustedDevicesError = nil
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/2fa/trusted-devices/revoke-all", method: .post)
            if response.ok { trustedDevices = []; return true }
            trustedDevicesError = response.error ?? "Couldn't forget your devices."
        } catch let error as APIClient.APIError {
            trustedDevicesError = error.message
        } catch {
            trustedDevicesError = "Couldn't forget your devices."
        }
        return false
    }

    private struct RecoveryEmailBody: Encodable { let email: String }
    private struct RecoveryCodeBody: Encodable { let code: String }
    var isRecoveryEmailBusy = false
    var recoveryEmailError: String?

    func startRecoveryEmail(_ email: String) async -> Bool {
        isRecoveryEmailBusy = true; recoveryEmailError = nil
        defer { isRecoveryEmailBusy = false }
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/recovery-email", method: .post, body: RecoveryEmailBody(email: email))
            if response.ok { await load(); return true }
            recoveryEmailError = response.error ?? "Couldn't send the code."
        } catch let error as APIClient.APIError {
            recoveryEmailError = error.message
        } catch {
            recoveryEmailError = "Couldn't send the code."
        }
        return false
    }

    func verifyRecoveryEmail(code: String) async -> Bool {
        isRecoveryEmailBusy = true; recoveryEmailError = nil
        defer { isRecoveryEmailBusy = false }
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/recovery-email/verify", method: .post, body: RecoveryCodeBody(code: code))
            if response.ok { await load(); return true }
            recoveryEmailError = response.error ?? "That code didn't work."
        } catch let error as APIClient.APIError {
            recoveryEmailError = error.message
        } catch {
            recoveryEmailError = "That code didn't work."
        }
        return false
    }

    func removeRecoveryEmail() async -> Bool {
        isRecoveryEmailBusy = true; recoveryEmailError = nil
        defer { isRecoveryEmailBusy = false }
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/recovery-email/remove", method: .post)
            if response.ok { await load(); return true }
            recoveryEmailError = response.error ?? "Couldn't remove it."
        } catch let error as APIClient.APIError {
            recoveryEmailError = error.message
        } catch {
            recoveryEmailError = "Couldn't remove it."
        }
        return false
    }

    private struct AutoApproveBody: Encodable {
        let enabled: Bool
        let paused: Bool
        let dailyCap: Int
        enum CodingKeys: String, CodingKey { case enabled, paused; case dailyCap = "daily_cap" }
    }
    var isSavingAutoApprove = false
    var autoApproveError: String?

    func saveAutoApprove(enabled: Bool, paused: Bool, dailyCap: Int) async -> Bool {
        isSavingAutoApprove = true; autoApproveError = nil
        defer { isSavingAutoApprove = false }
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/auto-approve", method: .post,
                                                                   body: AutoApproveBody(enabled: enabled, paused: paused, dailyCap: dailyCap))
            if response.ok { await load(); return true }
            autoApproveError = response.error ?? "Couldn't save that."
        } catch let error as APIClient.APIError {
            autoApproveError = error.message
        } catch {
            autoApproveError = "Couldn't save that."
        }
        return false
    }

    private struct HoursBody: Encodable { let open: [String: String]; let close: [String: String]; let closures: [String] }
    var isSavingHours = false
    var saveHoursError: String?

    func saveHours(open: [String: String], close: [String: String], closures: [String]) async -> Bool {
        isSavingHours = true; saveHoursError = nil
        defer { isSavingHours = false }
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/hours", method: .post,
                                                                   body: HoursBody(open: open, close: close, closures: closures))
            if response.ok { await load(); return true }
            saveHoursError = response.error ?? "Couldn't save your hours."
        } catch let error as APIClient.APIError {
            saveHoursError = error.message
        } catch {
            saveHoursError = "Couldn't save your hours."
        }
        return false
    }

    private struct RetentionBody: Encodable { let months: Int }
    var isSavingRetention = false
    var retentionError: String?

    func setDataRetention(months: Int) async -> Bool {
        isSavingRetention = true; retentionError = nil
        defer { isSavingRetention = false }
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/data-retention", method: .post, body: RetentionBody(months: months))
            if response.ok { await load(); return true }
            retentionError = response.error ?? "Couldn't save that."
        } catch let error as APIClient.APIError {
            retentionError = error.message
        } catch {
            retentionError = "Couldn't save that."
        }
        return false
    }

    private struct BugReportBody: Encodable {
        let message: String
        let build: String
        let device: String
        let appVersion: String
        enum CodingKeys: String, CodingKey { case message, build, device; case appVersion = "app_version" }
    }
    var isReportingBug = false
    var reportBugError: String?

    func reportBug(message: String, build: String, device: String) async -> Bool {
        isReportingBug = true; reportBugError = nil
        defer { isReportingBug = false }
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/report-bug", method: .post,
                                                                   body: BugReportBody(message: message, build: build, device: device, appVersion: version))
            if response.ok { return true }
            reportBugError = response.error ?? "Couldn't send that."
        } catch let error as APIClient.APIError {
            reportBugError = error.message
        } catch {
            reportBugError = "Couldn't send that."
        }
        return false
    }
}
