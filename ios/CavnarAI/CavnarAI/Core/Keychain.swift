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
        if status == errSecSuccess { return true }
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
