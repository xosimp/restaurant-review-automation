import CryptoKit
import Foundation

/// The device-local app passcode — the fallback that keeps the re-entry
/// lock meaningful when "Require Face ID to reopen" is switched off (until
/// this existed, turning Face ID off left nothing at all between a picked-
/// up phone and the restaurant's numbers).
///
/// Only a salted SHA-256 of the code is ever stored, in the Keychain (never
/// UserDefaults — see Keychain's own doc comment). A 6-digit code has a
/// million possibilities, so the hash alone isn't what makes this hard to
/// brute-force; the attempt throttle below is. Both halves are cleared on
/// sign-out — the passcode belongs to the session that set it.
enum AppPasscode {
    static let length = 6

    private enum Key {
        static let hash = "cavnar.app_passcode_hash"
        static let salt = "cavnar.app_passcode_salt"
        static let failures = "cavnar.app_passcode_failures"
        static let lockoutUntil = "cavnar.app_passcode_lockout_until"
    }

    static var isSet: Bool { Keychain.get(Key.hash) != nil }

    static func set(_ code: String) {
        var saltBytes = [UInt8](repeating: 0, count: 16)
        _ = SecRandomCopyBytes(kSecRandomDefault, saltBytes.count, &saltBytes)
        let salt = Data(saltBytes).base64EncodedString()
        Keychain.set(salt, for: Key.salt)
        Keychain.set(digest(code, salt: salt), for: Key.hash)
        resetFailures()
    }

    static func clear() {
        Keychain.delete(Key.hash)
        Keychain.delete(Key.salt)
        resetFailures()
    }

    /// Pure check, no throttle bookkeeping — SessionStore.unlockWithPasscode
    /// wraps this with the attempt counter.
    static func matches(_ code: String) -> Bool {
        guard let salt = Keychain.get(Key.salt), let stored = Keychain.get(Key.hash) else { return false }
        return constantTimeEquals(digest(code, salt: salt), stored)
    }

    // MARK: - Attempt throttle

    /// Persisted (Keychain, not memory) so force-quitting the app doesn't
    /// reset the counter. 5 misses earns 30s, 8 earns 2 minutes, 10+ earns
    /// 10 minutes — enough to make guessing pointless, short enough that a
    /// fumbled entry after a long shift isn't a punishment.
    static var failures: Int {
        Int(Keychain.get(Key.failures) ?? "") ?? 0
    }

    static var lockoutRemaining: TimeInterval {
        guard let raw = Keychain.get(Key.lockoutUntil), let until = TimeInterval(raw) else { return 0 }
        return max(0, until - Date().timeIntervalSince1970)
    }

    static func recordFailure() {
        let count = failures + 1
        Keychain.set(String(count), for: Key.failures)
        let penalty: TimeInterval
        switch count {
        case ..<5: penalty = 0
        case 5..<8: penalty = 30
        case 8..<10: penalty = 120
        default: penalty = 600
        }
        if penalty > 0 {
            Keychain.set(String(Date().timeIntervalSince1970 + penalty), for: Key.lockoutUntil)
        }
    }

    static func resetFailures() {
        Keychain.delete(Key.failures)
        Keychain.delete(Key.lockoutUntil)
    }

    // MARK: - Hashing

    private static func digest(_ code: String, salt: String) -> String {
        let data = Data((salt + ":" + code).utf8)
        return Data(SHA256.hash(data: data)).base64EncodedString()
    }

    /// Compares every byte regardless of where the first mismatch is, so the
    /// time taken can't hint at how close a guess was.
    private static func constantTimeEquals(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        guard x.count == y.count else { return false }
        var diff: UInt8 = 0
        for i in x.indices { diff |= x[i] ^ y[i] }
        return diff == 0
    }
}
