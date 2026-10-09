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

    // Email history — what we've actually sent for this restaurant
    var emailHistory: [SentEmail] = []
    var isLoadingEmailHistory = false

    // Export my data
    var isExportingData = false
    var exportDataError: String?
    var exportDataSucceeded = false
    var isRequestingDeletion = false
    var deletionRequestError: String?

    // Self-serve test digest
    var isSendingTestDigest = false
    var testDigestError: String?
    var testDigestSucceeded = false

    // Marketing opt-out
    var isTogglingMarketingOptOut = false
    var isTogglingMonthlyReview = false

    // 2FA backup codes
    var backupCodesRemaining: Int?
    var isBackupCodesBusy = false
    var backupCodesError: String?

    // Team (invite / manage access)
    var teamMembers: [TeamMember] = []
    var teamAccessOptions: [TeamAccessOption] = []
    var teamRoleOptions: [TeamRoleOption] = []
    var canEditTeamAccess = false
    var teamAccessError: String?
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
            RestaurantClock.learn(summary?.profile.timezone)
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
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
        } catch let error as APIClient.APIError where error.status == 403 {
            // Billing is the account owner's (403 owner_only). Read as "No
            // active subscription" it told a manager the restaurant was not
            // paying; the server's own sentence says what is true.
            billing = BillingSummary(ok: false, reason: "owner_only", status: nil, nextDate: nil, amount: nil,
                                     paymentMethod: nil, portalURL: nil, message: error.message, invoices: nil)
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

    private typealias OKErrorResponse = APIClient.OKResponse

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
    var isTogglingLoginNotify = false

    @discardableResult
    func toggleLoginNotify(_ enabled: Bool) async -> Bool {
        isTogglingLoginNotify = true
        accountToggleError = nil
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
            accountToggleError = Self.toggleFailure(error)
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
        accountToggleError = nil
        defer { isTogglingMarketingOptOut = false }
        do {
            _ = try await client.send(
                "/mobile/api/account/marketing-opt-out", method: .post, body: MarketingOptOutBody(optedOut: optedOut)
            ) as APIClient.EmptyResponse
            summary?.account.marketingEmailsOptOut = optedOut
            return true
        } catch {
            accountToggleError = Self.toggleFailure(error)
            await load()
            return false
        }
    }

    private struct EnabledBody: Encodable { let enabled: Bool }

    /// The monthly business review on or off — POST /account/monthly-review,
    /// the web switch's shared body (client_api._do_monthly_review_pref).
    @discardableResult
    func toggleMonthlyReview(_ enabled: Bool) async -> Bool {
        isTogglingMonthlyReview = true
        accountToggleError = nil
        defer { isTogglingMonthlyReview = false }
        do {
            _ = try await client.send(
                "/mobile/api/account/monthly-review", method: .post, body: EnabledBody(enabled: enabled)
            ) as APIClient.EmptyResponse
            summary?.account.monthlyReviewEnabled = enabled
            return true
        } catch {
            accountToggleError = Self.toggleFailure(error)
            await load()
            return false
        }
    }

    /// Why the last account switch (sign-in notifications, monthly review,
    /// product emails) didn't take. They used to snap back with nothing
    /// said — an owner_only refusal included.
    var accountToggleError: String?

    static func toggleFailure(_ error: Error) -> String {
        (error as? APIClient.APIError)?.message ?? "Couldn't save that change. Check your connection and try again."
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

    private struct EmailHistoryResponse: Decodable { let ok: Bool; let emails: [SentEmail] }

    func loadEmailHistory() async {
        isLoadingEmailHistory = true
        defer { isLoadingEmailHistory = false }
        do {
            let response: EmailHistoryResponse = try await client.send(
                "/mobile/api/account/email-history", hapticOnError: false)
            emailHistory = response.emails
        } catch {
            // Non-fatal — the sheet shows its empty state.
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

    /// Account -> Close my account. Not self-serve deletion — Cavnar AI
    /// clients are under a service contract — but this is a real request
    /// now, not a mailto: link. Reloads summary on success (the established
    /// pattern here, same as updateProfile below) so the sheet immediately
    /// reflects deletion_requested_at without a separate patch path.
    private struct DeletionRequestResponse: Decodable { let ok: Bool; let requestedAt: String?; let error: String?
        enum CodingKeys: String, CodingKey { case ok, error; case requestedAt = "requested_at" }
    }

    func requestAccountDeletion() async -> Bool {
        isRequestingDeletion = true
        deletionRequestError = nil
        defer { isRequestingDeletion = false }
        do {
            let response: DeletionRequestResponse = try await client.send("/mobile/api/account/request-deletion", method: .post)
            if response.ok {
                await load()
                return true
            }
            deletionRequestError = response.error ?? "Couldn't send that request."
            return false
        } catch let error as APIClient.APIError {
            deletionRequestError = error.message
            return false
        } catch {
            deletionRequestError = "Couldn't send that request."
            return false
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

    // MARK: - Staff accounts (the employee PIN portal)
    //
    // The owner side of staff_routes.py. Most employees sign themselves up
    // with the join code, so the common jobs here are watching who claimed
    // which name and correcting it — not creating accounts by hand.

    var staffAccounts: [StaffAccount] = []
    var staffJoinCode: String = ""
    var staffPortalURL: String = ""
    /// Names on the roster that nobody has claimed yet — the question an
    /// owner actually asks when they open this screen.
    var staffUnclaimed: [String] = []
    var isLoadingStaff = false
    var staffError: String?

    struct StaffAccount: Decodable, Identifiable, Hashable {
        let membershipID: Int
        let name: String
        let jobRole: String?
        let active: Bool
        let hasPin: Bool
        let locked: Bool
        let claimedByPhone: String?
        let claimedAt: String?
        let selfSignup: Bool

        var id: Int { membershipID }

        enum CodingKeys: String, CodingKey {
            case name, active, locked
            case membershipID = "membership_id"
            case jobRole = "job_role"
            case hasPin = "has_pin"
            case claimedByPhone = "claimed_by_phone"
            case claimedAt = "claimed_at"
            case selfSignup = "self_signup"
        }
    }

    /// One failed or locked-out staff PIN (auth.get_pin_security_events) —
    /// the web's "Recent failed PINs" (parity #70).
    struct PinEvent: Decodable, Identifiable, Hashable {
        let event: String
        let name: String?
        let createdAt: String
        var id: String { createdAt + event + (name ?? "") }
        var isLockout: Bool { event == "pin_locked" }
        enum CodingKeys: String, CodingKey {
            case event, name
            case createdAt = "created_at"
        }
    }
    var pinEvents: [PinEvent] = []

    private struct StaffListResponse: Decodable {
        let ok: Bool
        let staff: [StaffAccount]
        let joinCode: String?
        let portalURL: String?
        let unclaimed: [String]?
        let pinEvents: [PinEvent]?

        enum CodingKeys: String, CodingKey {
            case ok, staff, unclaimed
            case joinCode = "join_code"
            case portalURL = "portal_url"
            case pinEvents = "pin_events"
        }
    }

    func loadStaff() async {
        isLoadingStaff = true
        defer { isLoadingStaff = false }
        do {
            let response: StaffListResponse = try await client.send(
                "/mobile/api/account/staff", hapticOnError: false)
            staffAccounts = response.staff
            staffJoinCode = response.joinCode ?? ""
            staffPortalURL = response.portalURL ?? ""
            staffUnclaimed = response.unclaimed ?? []
            pinEvents = response.pinEvents ?? []
        } catch {
            // Non-fatal — the sheet shows its empty state.
        }
    }

    private typealias StaffOK = APIClient.OKResponse

    @discardableResult
    private func staffAction(_ path: String, body: (any Encodable)? = nil,
                             failure: String) async -> Bool {
        staffError = nil
        do {
            let response: StaffOK = try await client.send(path, method: .post, body: body)
            if response.ok {
                await loadStaff()
                return true
            }
            staffError = response.error ?? failure
            return false
        } catch let error as APIClient.APIError {
            staffError = error.message
            return false
        } catch {
            staffError = failure
            return false
        }
    }

    private struct StaffCreateBody: Encodable {
        let employeeName: String
        let jobRole: String
        let pin: String
        enum CodingKeys: String, CodingKey {
            case pin
            case employeeName = "employee_name"
            case jobRole = "job_role"
        }
    }

    @discardableResult
    func createStaff(name: String, jobRole: String, pin: String) async -> Bool {
        await staffAction("/mobile/api/account/staff",
                          body: StaffCreateBody(employeeName: name, jobRole: jobRole, pin: pin),
                          failure: "Couldn't add that employee.")
    }

    private struct StaffPatchBody: Encodable {
        var employeeName: String?
        var jobRole: String?
        var active: Bool?
        enum CodingKeys: String, CodingKey {
            case active
            case employeeName = "employee_name"
            case jobRole = "job_role"
        }
    }

    @discardableResult
    func renameStaff(_ membershipID: Int, to name: String) async -> Bool {
        await staffAction("/mobile/api/account/staff/\(membershipID)",
                          body: StaffPatchBody(employeeName: name),
                          failure: "Couldn't save that name.")
    }

    @discardableResult
    func retitleStaff(_ membershipID: Int, to jobRole: String) async -> Bool {
        await staffAction("/mobile/api/account/staff/\(membershipID)",
                          body: StaffPatchBody(jobRole: jobRole),
                          failure: "Couldn't save that job.")
    }

    @discardableResult
    func setStaffActive(_ membershipID: Int, active: Bool) async -> Bool {
        await staffAction("/mobile/api/account/staff/\(membershipID)",
                          body: StaffPatchBody(active: active),
                          failure: "Couldn't change that account.")
    }

    private struct StaffPinBody: Encodable { let pin: String }

    @discardableResult
    func resetStaffPin(_ membershipID: Int, pin: String) async -> Bool {
        await staffAction("/mobile/api/account/staff/\(membershipID)/pin",
                          body: StaffPinBody(pin: pin),
                          failure: "Couldn't set that PIN.")
    }

    @discardableResult
    func unlockStaff(_ membershipID: Int) async -> Bool {
        await staffAction("/mobile/api/account/staff/\(membershipID)/unlock",
                          failure: "Couldn't unlock that account.")
    }

    /// The owner's undo for a name claimed by the wrong person: the name goes
    /// back on the list for whoever it belongs to, and the phone that took it
    /// is refused if it tries again.
    @discardableResult
    func unlinkStaff(_ membershipID: Int) async -> Bool {
        await staffAction("/mobile/api/account/staff/\(membershipID)/unlink",
                          failure: "Couldn't unlink that account.")
    }

    @discardableResult
    func rotateStaffJoinCode() async -> Bool {
        await staffAction("/mobile/api/account/staff/portal-link/rotate",
                          failure: "Couldn't make a new code.")
    }

    private struct TeamResponse: Decodable {
        let ok: Bool
        let members: [TeamMember]
        let accessOptions: [TeamAccessOption]?
        let roleOptions: [TeamRoleOption]?
        let canEditAccess: Bool?
        enum CodingKeys: String, CodingKey {
            case ok, members
            case accessOptions = "access_options"
            case roleOptions = "role_options"
            case canEditAccess = "can_edit_access"
        }
    }

    func loadTeam() async {
        isLoadingTeam = true
        defer { isLoadingTeam = false }
        do {
            let response: TeamResponse = try await client.send("/mobile/api/account/team", hapticOnError: false)
            teamMembers = response.members
            teamAccessOptions = response.accessOptions ?? []
            teamRoleOptions = response.roleOptions ?? []
            canEditTeamAccess = response.canEditAccess ?? false
        } catch {
            // Non-fatal — sheet just shows an empty state.
        }
    }

    private struct RoleBody: Encodable { let role: String }

    /// Co-owner, Manager or Teammate. Reloads the team on success, since a
    /// role change also changes what access switches apply.
    @discardableResult
    func setTeamRole(_ userID: Int, role: String) async -> Bool {
        teamAccessError = nil
        do {
            let r: AccessResponse = try await client.send(
                "/mobile/api/account/team/\(userID)/role", method: .post, body: RoleBody(role: role))
            guard r.ok else {
                teamAccessError = r.error ?? "Couldn't change their role."
                return false
            }
            await loadTeam()
            return true
        } catch let error as APIClient.APIError {
            teamAccessError = error.message
            return false
        } catch {
            teamAccessError = "Couldn't change their role."
            return false
        }
    }

    /// Only the keys being changed are encoded (nil is omitted): the route
    /// acts on each key present, so a morning-brief tap never touches the
    /// nightly report and the reverse.
    struct AccessBody: Encodable, Equatable {
        var permission: String? = nil
        var enabled: Bool? = nil
        var morningBrief: Bool? = nil
        var nightlyReport: Bool? = nil
        enum CodingKeys: String, CodingKey {
            case permission, enabled
            case morningBrief = "morning_brief"
            case nightlyReport = "nightly_report"
        }
    }
    private struct AccessResponse: Decodable {
        let ok: Bool
        let access: [String]?
        let morningBrief: Bool?
        let nightlyReport: Bool?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, access, error
            case morningBrief = "morning_brief"
            case nightlyReport = "nightly_report"
        }
    }

    /// Open or close one area (food cost, comps & voids) to a manager, turn
    /// their morning brief on/off, or leave them off the nightly sales report
    /// (the web's "Nightly report" switch, parity #70). Updates that row from
    /// the server's answer rather than trusting the tap.
    @discardableResult
    func setTeamAccess(_ userID: Int, permission: String? = nil, enabled: Bool? = nil,
                       morningBrief: Bool? = nil, nightlyReport: Bool? = nil) async -> Bool {
        teamAccessError = nil
        do {
            let r: AccessResponse = try await client.send(
                "/mobile/api/account/team/\(userID)/access", method: .post,
                body: AccessBody(permission: permission, enabled: enabled, morningBrief: morningBrief,
                                 nightlyReport: nightlyReport))
            guard r.ok else {
                teamAccessError = r.error ?? "Couldn't update their access."
                return false
            }
            if let i = teamMembers.firstIndex(where: { $0.id == userID }) {
                teamMembers[i].access = r.access ?? []
                teamMembers[i].morningBrief = r.morningBrief
                if let nightly = r.nightlyReport { teamMembers[i].nightlyReport = nightly }
            }
            return true
        } catch let error as APIClient.APIError {
            teamAccessError = error.message
            return false
        } catch {
            teamAccessError = "Couldn't update their access."
            return false
        }
    }

    private struct InviteBody: Encodable { let name: String; let email: String; let role: String }
    private typealias InviteResponse = APIClient.OKResponse
    /// The invite route's answer: the login is made either way, and whether
    /// the email carrying their sign-in went out (re-audit A3).
    private struct InviteResult: Decodable {
        let ok: Bool
        let error: String?
        let inviteEmailSent: Bool?
        let inviteEmailError: String?
        enum CodingKeys: String, CodingKey {
            case ok, error
            case inviteEmailSent = "invite_email_sent"
            case inviteEmailError = "invite_email_error"
        }
    }
    /// Set when the teammate was added but their invite email didn't go out
    /// — the server's sentence, shown instead of "Invite sent".
    var inviteEmailNotice: String?

    /// "The invite email…" → "the invite email…", to follow "X is on the team, but".
    static func lowerFirst(_ s: String) -> String {
        guard let f = s.first else { return s }
        return f.lowercased() + s.dropFirst()
    }

    @discardableResult
    func inviteTeamMember(name: String, email: String, role: String = "manager") async -> Bool {
        isInvitingTeamMember = true
        inviteTeamError = nil
        inviteEmailNotice = nil
        defer { isInvitingTeamMember = false }
        do {
            let response: InviteResult = try await client.send(
                "/mobile/api/account/team/invite", method: .post, body: InviteBody(name: name, email: email, role: role)
            )
            if response.ok {
                if response.inviteEmailSent == false {
                    let reason = response.inviteEmailError
                        ?? "The invite email didn\u{2019}t go out \u{2014} try again or tell them yourself."
                    inviteEmailNotice = "\(name.trimmingCharacters(in: .whitespaces)) is on the team, but "
                        + Self.lowerFirst(reason)
                }
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

    struct AlertContactBody: Encodable {
        let name: String
        let phone: String
    }

    struct AlertSettingsBody: Encodable {
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
        let alertCompetitorMove: Bool
        let alertExtraEmails: String
        let pushSound: Bool
        let contacts: [AlertContactBody]
        /// The web's four — sent only when the server sent them, so the
        /// route's "only when sent" rule keeps an older payload's value.
        var alertRatingThreshold: Bool? = nil
        var alertRatingFloor: Double? = nil
        var alertAnyReview: Bool? = nil
        var alertRespApproved: Bool? = nil

        enum CodingKeys: String, CodingKey {
            case alertRatingThreshold = "alert_rating_threshold"
            case alertRatingFloor = "alert_rating_floor"
            case alertAnyReview = "alert_any_review"
            case alertRespApproved = "alert_resp_approved"
            case alertHealthBypassQuiet = "alert_health_bypass_quiet"
            case alertFoodWaste = "alert_food_waste"
            case alertAiVisibilityDrop = "alert_ai_visibility_drop"
            case alertCompetitorMove = "alert_competitor_move"
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
        let body = Self.alertSettingsBody(settings, contacts: contacts)
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

    /// The body the Alerts sheet posts — pure, so its keys are pinned by a test.
    static func alertSettingsBody(_ settings: AlertSettings, contacts: [AlertContact]) -> AlertSettingsBody {
        var body = AlertSettingsBody(
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
            alertCompetitorMove: settings.alertCompetitorMove,
            alertExtraEmails: settings.alertExtraEmails,
            pushSound: settings.pushSound,
            contacts: contacts.map { AlertContactBody(name: $0.name, phone: $0.phone) }
        )
        body.alertRatingThreshold = settings.alertRatingThreshold
        body.alertRatingFloor = settings.alertRatingFloor
        body.alertAnyReview = settings.alertAnyReview
        body.alertRespApproved = settings.alertRespApproved
        return body
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

    /// Every field optional, and a nil one is left out of the JSON
    /// (synthesized encodeIfPresent): the route writes only the keys sent, so
    /// the contact save sends only what changed and never puts a brand voice,
    /// menu, language or time zone from the phone's cache back over a web
    /// edit (re-audit A2).
    private struct UpdateProfileBody: Encodable {
        var ownerName: String? = nil
        var ownerPhone: String? = nil
        var voiceNotes: String? = nil
        var neverSay: String? = nil
        var menuNotes: String? = nil
        var timezone: String? = nil
        var signOffName: String? = nil
        var responseLanguage: String? = nil
        enum CodingKeys: String, CodingKey {
            case signOffName = "sign_off_name"
            case responseLanguage = "response_language"
            case ownerName = "owner_name"
            case ownerPhone = "owner_phone"
            case voiceNotes = "voice_notes"
            case neverSay = "never_say"
            case menuNotes = "menu_notes"
            case timezone
        }
    }

    /// Saves the owner's contact fields — only those passed (nil = unchanged).
    func updateProfile(ownerName: String? = nil, ownerPhone: String? = nil) async {
        guard ownerName != nil || ownerPhone != nil else { saveProfileSucceeded = true; return }
        isSavingProfile = true
        saveProfileError = nil
        saveProfileSucceeded = false
        defer { isSavingProfile = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/account/update-profile", method: .post,
                body: UpdateProfileBody(ownerName: ownerName, ownerPhone: ownerPhone)
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

    // MARK: - Instagram & Facebook (Meta OAuth, same browser-sheet flow)

    var isConnectingInstagram = false
    var connectInstagramError: String?

    func connectInstagram() async {
        isConnectingInstagram = true
        connectInstagramError = nil
        defer { isConnectingInstagram = false }
        do {
            let response: GoogleAuthorizeResponse = try await client.send("/mobile/api/connections/instagram/authorize")
            guard response.ok, let urlString = response.url, let url = URL(string: urlString) else {
                connectInstagramError = response.error ?? "Couldn't start Instagram connect."
                return
            }
            try await GMBConnectCoordinator().connect(authorizeURL: url)
            await load()
        } catch let error as GMBConnectError {
            switch error {
            case .cancelled: break
            case .server(let msg): connectInstagramError = msg
            }
        } catch let error as APIClient.APIError {
            connectInstagramError = error.message
        } catch {
            connectInstagramError = "Couldn't connect Instagram & Facebook."
        }
    }

    @discardableResult
    func disconnectInstagram() async -> Bool { await disconnect("/mobile/api/connections/instagram", name: "Instagram & Facebook") }

    // MARK: - Referral

    private struct ReferralBody: Encodable {
        let name: String
        let email: String
        let note: String
    }

    /// The web's "Know another restaurant owner?" card — one free month
    /// if they sign up. Same route, same email.
    func sendReferral(name: String, email: String, note: String) async -> String? {
        do {
            let response: APIClient.EmptyResponse = try await client.send(
                "/mobile/api/account/referral", method: .post,
                body: ReferralBody(name: name, email: email, note: note))
            _ = response
            return nil
        } catch let error as APIClient.APIError {
            return error.message
        } catch {
            return "Couldn't send that right now."
        }
    }

    @discardableResult
    func disconnectGoogleBusiness() async -> Bool { await disconnect("/mobile/api/connections/google", name: "Google Business") }

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

    @discardableResult
    func disconnectToast() async -> Bool { await disconnect("/mobile/api/connections/toast", name: "Toast") }

    // Connections — Square / Clover
    //
    // Both mirror Toast exactly: a credential pair posted to a mobile route
    // that verifies it against the POS before storing, so a typo fails in
    // the sheet instead of silently at the next nightly sync. These rows
    // used to be status-only because square_routes.py/clover_routes.py are
    // session-auth (@login_required) and the app could never reach them.

    var isConnectingSquare = false
    var connectSquareError: String?
    var connectSquareSucceeded = false

    private struct ConnectSquareBody: Encodable {
        let squareAccessToken: String
        let squareLocationId: String
        enum CodingKeys: String, CodingKey {
            case squareAccessToken = "square_access_token"
            case squareLocationId = "square_location_id"
        }
    }

    func connectSquare(accessToken: String, locationId: String) async {
        isConnectingSquare = true
        connectSquareError = nil
        connectSquareSucceeded = false
        defer { isConnectingSquare = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/connections/square", method: .post,
                body: ConnectSquareBody(squareAccessToken: accessToken, squareLocationId: locationId)
            )
            if response.ok {
                connectSquareSucceeded = true
                await load()
            } else {
                connectSquareError = response.error ?? "Couldn't connect Square."
            }
        } catch let error as APIClient.APIError {
            connectSquareError = error.message
        } catch {
            connectSquareError = "Couldn't connect Square."
        }
    }

    @discardableResult
    func disconnectSquare() async -> Bool { await disconnect("/mobile/api/connections/square", name: "Square") }

    var isConnectingClover = false
    var connectCloverError: String?
    var connectCloverSucceeded = false

    private struct ConnectCloverBody: Encodable {
        let cloverMerchantId: String
        let cloverApiToken: String
        enum CodingKeys: String, CodingKey {
            case cloverMerchantId = "clover_merchant_id"
            case cloverApiToken = "clover_api_token"
        }
    }

    func connectClover(merchantId: String, apiToken: String) async {
        isConnectingClover = true
        connectCloverError = nil
        connectCloverSucceeded = false
        defer { isConnectingClover = false }
        do {
            let response: OKErrorResponse = try await client.send(
                "/mobile/api/connections/clover", method: .post,
                body: ConnectCloverBody(cloverMerchantId: merchantId, cloverApiToken: apiToken)
            )
            if response.ok {
                connectCloverSucceeded = true
                await load()
            } else {
                connectCloverError = response.error ?? "Couldn't connect Clover."
            }
        } catch let error as APIClient.APIError {
            connectCloverError = error.message
        } catch {
            connectCloverError = "Couldn't connect Clover."
        }
    }

    @discardableResult
    func disconnectClover() async -> Bool { await disconnect("/mobile/api/connections/clover", name: "Clover") }

    // MARK: - Settings audit additions

    private struct ActivityResponse: Decodable {
        let ok: Bool
        let events: [AccountActivityEvent]
        /// The lasting, attributed change history (change_log.for_viewer —
        /// memory round 9/29/26, M7). Read element by element; absent on
        /// an older server.
        var changes: HomeLenientList<AccountChange>? = nil
    }
    var activity: [AccountActivityEvent] = []
    /// "Labor target: 30 → 28, by the owner on 9/12/26", newest first.
    var changes: [AccountChange] = []
    var isLoadingActivity = false

    func loadActivity() async {
        isLoadingActivity = true
        defer { isLoadingActivity = false }
        do {
            let response: ActivityResponse = try await client.send("/mobile/api/account/activity", hapticOnError: false)
            activity = response.events
            changes = response.changes?.items ?? []
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

    /// Every key client_api._do_auto_approve reads — include_4star among
    /// them: the shared body sets auto_approve_4star from it, so a body
    /// without it switched the owner's 4-star rule off on every save
    /// (parity #2). Pinned by AccountParityTests and tests/test_ios_account_parity.py.
    struct AutoApproveBody: Encodable, Equatable {
        let enabled: Bool
        let paused: Bool
        let dailyCap: Int
        let earned: Bool
        let include4star: Bool
        enum CodingKeys: String, CodingKey {
            case enabled, paused, earned
            case dailyCap = "daily_cap"
            case include4star = "include_4star"
        }
    }
    var isSavingAutoApprove = false
    var autoApproveError: String?

    func saveAutoApprove(enabled: Bool, paused: Bool, dailyCap: Int, earned: Bool = false,
                         include4star: Bool) async -> Bool {
        isSavingAutoApprove = true; autoApproveError = nil
        defer { isSavingAutoApprove = false }
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/auto-approve", method: .post,
                                                                   body: AutoApproveBody(enabled: enabled, paused: paused, dailyCap: dailyCap,
                                                                                         earned: earned, include4star: include4star))
            if response.ok { await load(); return true }
            autoApproveError = response.error ?? "Couldn't save that."
        } catch let error as APIClient.APIError {
            autoApproveError = error.message
        } catch {
            autoApproveError = "Couldn't save that."
        }
        return false
    }

    /// Open and close times only. Closed dates no longer ride on Save hours:
    /// each add or remove is its own save (/account/closures, below), as on
    /// the web — a list sent with Save could lose a date picked and not yet
    /// saved, or one added elsewhere since (parity #7, owner edits never vanish).
    struct HoursBody: Encodable, Equatable {
        let open: [String: String]; let close: [String: String]
    }
    var isSavingHours = false
    var saveHoursError: String?

    func saveHours(open: [String: String], close: [String: String]) async -> Bool {
        isSavingHours = true; saveHoursError = nil
        defer { isSavingHours = false }
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/hours", method: .post,
                                                                   body: HoursBody(open: open, close: close))
            if response.ok { await load(); return true }
            saveHoursError = response.error ?? "Couldn't save your hours."
        } catch let error as APIClient.APIError {
            saveHoursError = error.message
        } catch {
            saveHoursError = "Couldn't save your hours."
        }
        return false
    }

    /// One closed date added or removed — {"add": iso} or {"remove": iso}.
    struct ClosureChange: Encodable, Equatable {
        var add: String? = nil
        var remove: String? = nil
    }
    private struct ClosuresResponse: Decodable {
        let ok: Bool
        let closures: [String]?
        let error: String?
    }
    var closureBusy: String?
    var closureError: String?

    /// Saves one closed date on its own and answers the list as the server
    /// now holds it (dates added elsewhere included), nil when it failed.
    func changeClosure(_ change: ClosureChange) async -> [String]? {
        closureBusy = change.add ?? change.remove
        closureError = nil
        defer { closureBusy = nil }
        do {
            let r: ClosuresResponse = try await client.send("/mobile/api/account/closures", method: .post,
                                                            body: change, retryTransient: false)
            guard r.ok, let list = r.closures else {
                closureError = r.error ?? "Couldn't save that date."
                return nil
            }
            let saved = list.filter { !$0.isEmpty }.sorted()
            // The summary the Profile sheet reopens Hours from: without this
            // the sheet came back on the list it first opened with (re-audit A4).
            summary?.profile.closures = saved
            return saved
        } catch let error as APIClient.APIError {
            closureError = error.message
        } catch {
            closureError = "Couldn't reach the server \u{2014} that date isn't saved."
        }
        return nil
    }

    /// The opening hours Google Business lists, to fill the form with —
    /// nothing is saved until the owner taps Save hours (parity #88).
    struct GoogleHours: Decodable {
        let ok: Bool
        let open: [String: String]?
        let close: [String: String]?
        let error: String?
    }

    func hoursFromGoogle() async -> GoogleHours {
        do {
            return try await client.send("/mobile/api/account/hours/google", hapticOnError: false)
        } catch let error as APIClient.APIError {
            return GoogleHours(ok: false, open: nil, close: nil, error: error.message)
        } catch {
            return GoogleHours(ok: false, open: nil, close: nil, error: "Couldn't reach Google.")
        }
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

    /// Every key /account/report-bug files (mobile_api.mobile_report_bug's
    /// `meta`): `ios_version` and `screen` were never sent, so a phone's
    /// report reached the admin console without the iOS version or where it
    /// was filed from (parity audit #24, the request-body parity test).
    struct BugReportBody: Encodable, Equatable {
        let message: String
        let build: String
        let device: String
        let appVersion: String
        let iosVersion: String
        let screen: String
        enum CodingKeys: String, CodingKey {
            case message, build, device, screen
            case appVersion = "app_version"
            case iosVersion = "ios_version"
        }
    }
    var isReportingBug = false
    var reportBugError: String?

    func reportBug(message: String, build: String, device: String, iosVersion: String,
                   screen: String = "ios:account") async -> Bool {
        isReportingBug = true; reportBugError = nil
        defer { isReportingBug = false }
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        do {
            let response: OKErrorResponse = try await client.send("/mobile/api/account/report-bug", method: .post,
                                                                   body: BugReportBody(message: message, build: build, device: device,
                                                                                       appVersion: version, iosVersion: iosVersion,
                                                                                       screen: screen))
            if response.ok { return true }
            reportBugError = response.error ?? "Couldn't send that."
        } catch let error as APIClient.APIError {
            reportBugError = error.message
        } catch {
            reportBugError = "Couldn't send that."
        }
        return false
    }

    // MARK: - Staff sign-in notices (parity #70)

    var isTogglingStaffSignIn = false

    /// The web's "Tell me when someone opens the staff portal" — the
    /// account holder's (/account/staff-signin-notify).
    @discardableResult
    func toggleStaffSignInNotify(_ enabled: Bool) async -> Bool {
        isTogglingStaffSignIn = true
        staffError = nil
        defer { isTogglingStaffSignIn = false }
        do {
            _ = try await client.send("/mobile/api/account/staff-signin-notify", method: .post,
                                      body: EnabledBody(enabled: enabled)) as APIClient.OKResponse
            summary?.account.staffSignInNotify = enabled
            return true
        } catch {
            staffError = Self.toggleFailure(error)
            return false
        }
    }

    // MARK: - POS Sync now, RPOWER disconnect (parity #80)

    /// The provider whose sync is being started, and what the server said.
    var syncingProvider: String?
    var syncMessage: [String: String] = [:]

    private struct SyncResponse: Decodable { let ok: Bool; let message: String?; let error: String? }

    func syncNow(_ provider: String) async {
        syncingProvider = provider
        defer { syncingProvider = nil }
        do {
            let r: SyncResponse = try await client.send("/mobile/api/connections/\(provider)/sync", method: .post,
                                                        retryTransient: false)
            syncMessage[provider] = r.ok ? (r.message ?? "Sync started.") : (r.error ?? "Couldn't start a sync.")
            if r.ok { Haptic.success() }
        } catch let error as APIClient.APIError {
            syncMessage[provider] = error.message
        } catch {
            syncMessage[provider] = "Couldn't start a sync."
        }
    }

    var disconnectError: String?

    @discardableResult
    func disconnectRPower() async -> Bool { await disconnect("/mobile/api/connections/rpower", name: "RPOWER") }

    /// `{ok?, error?}` — some disconnect routes answer `{}`.
    private struct DisconnectResponse: Decodable { let ok: Bool?; let error: String? }

    /// Every disconnect: true only when the server took it off. A failure
    /// says so in `disconnectError` — they used to be swallowed, with a
    /// success haptic either way (re-audit A5).
    private func disconnect(_ path: String, name: String) async -> Bool {
        disconnectError = nil
        do {
            let r: DisconnectResponse = try await client.send(path, method: .delete, retryTransient: false)
            if r.ok == false {
                disconnectError = r.error ?? "Couldn\u{2019}t disconnect \(name)."
                return false
            }
            await load()
            return true
        } catch let error as APIClient.APIError {
            disconnectError = error.message
        } catch {
            disconnectError = "Couldn\u{2019}t disconnect \(name) \u{2014} check your connection and try again."
        }
        return false
    }

    // MARK: - Delete my login (a teammate's own; parity #13)

    var isDeletingLogin = false
    var deleteLoginError: String?

    /// True once the server removed this login — the caller then signs out.
    func deleteOwnLogin() async -> Bool {
        isDeletingLogin = true
        deleteLoginError = nil
        defer { isDeletingLogin = false }
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/account/delete-login", method: .post,
                                                                retryTransient: false)
            if r.ok { return true }
            deleteLoginError = r.error ?? "Couldn't delete your login."
        } catch let error as APIClient.APIError {
            deleteLoginError = error.message
        } catch {
            deleteLoginError = "Couldn't delete your login."
        }
        return false
    }

    // MARK: - Security checkup and account health, scored on the server

    var securitySummary: SecuritySummary?
    /// The checkup couldn't be read and there is nothing to show — said,
    /// with Try again, never a loading bar left up (re-audit 10/8/26, #12).
    var securitySummaryError: String?
    var health: AccountHealth?

    func loadSecuritySummary() async {
        do {
            let s: SecuritySummary = try await client.send("/mobile/api/account/security-summary", hapticOnError: false)
            if s.ok {
                securitySummary = s
                securitySummaryError = nil
            } else if securitySummary == nil {
                securitySummaryError = "The security checkup couldn\u{2019}t be loaded."
            }
        } catch is CancellationError {
        } catch let e as APIClient.APIError {
            if securitySummary == nil { securitySummaryError = e.message }
        } catch {
            if securitySummary == nil { securitySummaryError = "The security checkup couldn\u{2019}t be loaded." }
        }
    }

    func loadHealth() async {
        if let h: AccountHealth = try? await client.send("/mobile/api/account/health", hapticOnError: false), h.ok {
            health = h
        }
    }
}
