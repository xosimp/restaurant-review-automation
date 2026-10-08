import Foundation
import Security

/// Thin wrapper around the Keychain Services API for the two secrets this
/// app ever persists: the bearer session token and the 2FA "remember this
/// device" value. Never use UserDefaults for either — UserDefaults is
/// unencrypted plist storage, not appropriate for anything that grants
/// access to a restaurant's data.
enum Keychain {
    /// ThisDeviceOnly: the items never leave this phone — not in an
    /// encrypted iTunes/Finder backup, not in an iCloud backup restored onto
    /// a new device. A session token or a 2FA "remember this device" value
    /// that followed a backup to another phone would sign that phone in as
    /// this one (CLIENT-25). AfterFirstUnlock still lets a background refresh
    /// read the token while the phone is locked.
    private static var accessibility: CFString { kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly }

    /// Returns whether the value was stored. The status used to be
    /// discarded, so a failed write (a locked keychain, a full device) left
    /// a session that worked until the next launch and then vanished with
    /// nothing to say why.
    @discardableResult
    static func set(_ value: String, for key: String) -> Bool {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
        var item = query
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = accessibility
        let status = SecItemAdd(item as CFDictionary, nil)
        if status == errSecSuccess {
            if key == Key.sessionToken { mirrorSessionForExtensions(value) }
            return true
        }
        // The delete above can lose a race with another writer of the same
        // key; update in place rather than give up.
        if status == errSecDuplicateItem {
            let update: [String: Any] = [
                kSecValueData as String: data,
                kSecAttrAccessible as String: accessibility,
            ]
            let updated = SecItemUpdate(query as CFDictionary, update as CFDictionary)
            if updated == errSecSuccess { return true }
            log("update", key: key, status: updated)
            return false
        }
        log("add", key: key, status: status)
        return false
    }

    static func get(_ key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecReturnAttributes as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let item = result as? [String: Any],
              let data = item[kSecValueData as String] as? Data,
              let value = String(data: data, encoding: .utf8)
        else { return nil }
        // Items written before ThisDeviceOnly still carry the old,
        // backup-portable class. Rewrite them the first time they are read,
        // so an existing install is bound to this device too, not only the
        // next sign-in.
        if (item[kSecAttrAccessible as String] as? String) != (accessibility as String) {
            set(value, for: key)
        }
        return value
    }

    private static func log(_ operation: String, key: String, status: OSStatus) {
        #if DEBUG
        print("[keychain] \(operation) failed for \(key): \(status)")
        #endif
    }

    static func delete(_ key: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
        // Signed out: the Share extension's copy goes with it (#95).
        if key == Key.sessionToken { clearSharedSession() }
    }

    // MARK: - The Share extension's copy of the session (parity audit #95)

    /// "Send to Cavnar AI" from Mail or Files runs in its own process, which
    /// cannot read the app's own keychain items. The owner session is
    /// copied into one keychain access group the app and the Share extension
    /// both hold (project.yml: keychain-access-groups) — never the widget
    /// extension, which signs nothing in. Same ThisDeviceOnly class as the
    /// original. The copy carries the server it belongs to, so a Debug build
    /// pointed at a tunnel uploads there and nowhere else.
    static let sharedSessionAccount = "cavnar.shared.session"

    /// "<team>.ai.cavnar.CavnarAI.shared", from Info.plist (CavnarKeychainGroup).
    static var sharedAccessGroup: String? {
        guard let g = Bundle.main.object(forInfoDictionaryKey: "CavnarKeychainGroup") as? String,
              !g.isEmpty, !g.hasPrefix("$(") else { return nil }
        return g
    }

    struct SharedSession: Codable, Equatable {
        let token: String
        let baseURL: String
        enum CodingKeys: String, CodingKey {
            case token
            case baseURL = "base_url"
        }
    }

    /// Writes the copy when it differs from what is there. No-op without a
    /// shared group (an unsigned Simulator build).
    static func mirrorSessionForExtensions(_ token: String) {
        guard let group = sharedAccessGroup, !token.isEmpty else { return }
        let session = SharedSession(token: token, baseURL: AppEnvironment.baseURL.absoluteString)
        if readSharedSession(group: group) == session { return }
        guard let data = try? JSONEncoder().encode(session) else { return }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: sharedSessionAccount,
            kSecAttrAccessGroup as String: group,
        ]
        SecItemDelete(query as CFDictionary)
        var item = query
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = accessibility
        let status = SecItemAdd(item as CFDictionary, nil)
        if status != errSecSuccess { log("add shared", key: sharedSessionAccount, status: status) }
    }

    static func readSharedSession(group: String? = sharedAccessGroup) -> SharedSession? {
        guard let group else { return nil }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: sharedSessionAccount,
            kSecAttrAccessGroup as String: group,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(SharedSession.self, from: data)
    }

    static func clearSharedSession() {
        guard let group = sharedAccessGroup else { return }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: sharedSessionAccount,
            kSecAttrAccessGroup as String: group,
        ]
        SecItemDelete(query as CFDictionary)
    }

    enum Key {
        static let sessionToken = "cavnar.session_token"
        static let deviceRememberToken = "cavnar.2fa_device_token"
        static let deviceIdentity = "cavnar.device_identity"
        /// The employee tier's shift session. A separate slot from
        /// sessionToken on purpose — the two tiers must never be able to
        /// read each other's token, even by a typo'd key.
        static let staffSessionToken = "cavnar.staff_session_token"
    }

    /// A stable identifier for this physical device/install, generated
    /// once and persisted here — sent on every login-family request so
    /// the backend can recognize "this exact device signing in again" and
    /// replace its old session instead of piling up a new row in the
    /// Devices list on every fresh login (expired session, sign-out/back-
    /// in, reinstall). See auth.py's create_session() for the other half.
    static func deviceIdentity() -> String {
        if let existing = get(Key.deviceIdentity) { return existing }
        let fresh = UUID().uuidString
        set(fresh, for: Key.deviceIdentity)
        return fresh
    }
}
