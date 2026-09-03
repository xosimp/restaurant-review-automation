import Foundation
import Security

/// Thin wrapper around the Keychain Services API for the two secrets this
/// app ever persists: the bearer session token and the 2FA "remember this
/// device" value. Never use UserDefaults for either — UserDefaults is
/// unencrypted plist storage, not appropriate for anything that grants
/// access to a restaurant's data.
enum Keychain {
    static func set(_ value: String, for key: String) {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
        var attributes = query
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(attributes as CFDictionary, nil)
    }

    static func get(_ key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
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
