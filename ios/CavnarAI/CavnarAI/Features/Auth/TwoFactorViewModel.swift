import Foundation
import Observation

@Observable
@MainActor
final class TwoFactorViewModel {
    let pendingToken: String
    var maskedEmail: String
    /// Where the code went: "sms" or "email"; nil from an older server.
    var channel: String?
    var code = ""
    /// A saved backup code ("7F3A-92C1") instead of the 6-digit code. The
    /// server reads it in any case, with or without the dash.
    var useBackupCode = false
    var backupCode = ""
    var rememberDevice = true
    var isLoading = false
    var errorMessage: String?

    // Resend: a fresh code to the same place, then a cooldown so a double
    // tap doesn't spend the server's per-address resend budget.
    var isResending = false
    var resendCooldown = 0
    var resendNotice: String?
    static let resendCooldownSeconds = 30

    private let sessionStore: SessionStore
    private var cooldownTask: Task<Void, Never>?

    init(sessionStore: SessionStore, pendingToken: String, maskedEmail: String, channel: String? = nil) {
        self.sessionStore = sessionStore
        self.pendingToken = pendingToken
        self.maskedEmail = maskedEmail
        self.channel = channel
    }

    /// "Check your texts" / "Check your email" — the web page's wording.
    var heading: String {
        if useBackupCode { return "Use a backup code" }
        switch channel {
        case "sms": return "Check your texts"
        case "email": return "Check your email"
        default: return "Enter your code"
        }
    }

    var subheading: String {
        if useBackupCode {
            return "Enter one of the backup codes you saved when you turned on two-factor. Each code works once."
        }
        let to = maskedEmail.isEmpty ? "" : " to \(maskedEmail)"
        switch channel {
        case "sms": return "We texted a 6-digit code\(to)."
        case "email": return "We emailed a 6-digit code\(to)."
        default: return "We sent a 6-digit code\(to)."
        }
    }

    /// The backup code with spaces and dashes dropped, upper-cased — only
    /// used to decide whether Verify can be pressed; the server normalises
    /// what is sent the same way (models.normalize_backup_code).
    static func backupCodeCharacters(_ raw: String) -> String {
        raw.uppercased().filter { $0.isHexDigit }
    }

    var canSubmit: Bool {
        guard !isLoading else { return false }
        if useBackupCode {
            let stripped = backupCode.uppercased().filter { !$0.isWhitespace && $0 != "-" }
            return stripped.count == 8 && Self.backupCodeCharacters(stripped).count == 8
        }
        return code.trimmingCharacters(in: .whitespaces).count == 6
    }

    var canResend: Bool { !isResending && resendCooldown == 0 && !isLoading }

    func toggleBackupCode() {
        useBackupCode.toggle()
        errorMessage = nil
        resendNotice = nil
    }

    func submit() async {
        guard canSubmit else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        let entered = useBackupCode ? backupCode.trimmingCharacters(in: .whitespaces) : code
        do {
            try await sessionStore.verifyTwoFactor(
                pendingToken: pendingToken, code: entered, rememberDevice: rememberDevice
            )
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Something went wrong. Try again."
        }
    }

    func resend() async {
        guard canResend else { return }
        isResending = true
        errorMessage = nil
        resendNotice = nil
        defer { isResending = false }
        do {
            let r = try await sessionStore.resendTwoFactor(pendingToken: pendingToken)
            if r.ok {
                if let c = r.channel { channel = c }
                if let m = r.masked, !m.isEmpty { maskedEmail = m }
                code = ""
                resendNotice = channel == "sms" ? "New code texted." : channel == "email" ? "New code emailed." : "New code sent."
                startCooldown()
            } else {
                errorMessage = r.error ?? "Couldn't send a new code. Go back and sign in again."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't send a new code. Try again."
        }
    }

    private func startCooldown() {
        cooldownTask?.cancel()
        resendCooldown = Self.resendCooldownSeconds
        cooldownTask = Task { @MainActor [weak self] in
            while let self, self.resendCooldown > 0 {
                try? await Task.sleep(for: .seconds(1))
                if Task.isCancelled { return }
                self.resendCooldown -= 1
            }
        }
    }
}
