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
        /// The penalty and the monotonic clock reading it started at — the
        /// wall clock alone could be wound forward in Settings to skip it.
        static let lockoutPenalty = "cavnar.app_passcode_lockout_penalty"
        static let lockoutStartedMono = "cavnar.app_passcode_lockout_mono"
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
        return remaining(until: until,
                         penalty: Keychain.get(Key.lockoutPenalty).flatMap(TimeInterval.init),
                         startedMono: Keychain.get(Key.lockoutStartedMono).flatMap(TimeInterval.init),
                         nowWall: Date().timeIntervalSince1970, nowMono: monotonicNow())
    }

    /// Seconds since boot on a clock Settings cannot move (it keeps
    /// counting through sleep).
    static func monotonicNow() -> TimeInterval {
        TimeInterval(clock_gettime_nsec_np(CLOCK_MONOTONIC)) / 1_000_000_000
    }

    /// The lockout left (F3-18). Within one boot it runs on the monotonic
    /// clock, so changing the device's time neither ends nor extends it.
    /// After a reboot (the monotonic clock restarted below the stamp) it
    /// falls back to the wall-clock deadline, capped at the penalty itself —
    /// winding the clock BACK can't lengthen it past what was earned either.
    static func remaining(until: TimeInterval, penalty: TimeInterval?, startedMono: TimeInterval?,
                          nowWall: TimeInterval, nowMono: TimeInterval) -> TimeInterval {
        guard let penalty, let startedMono else { return max(0, until - nowWall) }
        if nowMono >= startedMono {
            return max(0, penalty - (nowMono - startedMono))
        }
        return min(penalty, max(0, until - nowWall))
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
            Keychain.set(String(penalty), for: Key.lockoutPenalty)
            Keychain.set(String(monotonicNow()), for: Key.lockoutStartedMono)
        }
    }

    static func resetFailures() {
        Keychain.delete(Key.failures)
        Keychain.delete(Key.lockoutUntil)
        Keychain.delete(Key.lockoutPenalty)
        Keychain.delete(Key.lockoutStartedMono)
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
