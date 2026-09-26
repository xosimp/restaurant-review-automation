import Foundation

/// A flag that SQLite stores as INTEGER and a payload may send raw.
///
/// JSONDecoder refuses a number for a `Bool`, and with synthesized Codable
/// one refused field fails the whole payload: a single review whose
/// `draft_needs_review` arrived as `0` emptied the entire inbox. Wrap any
/// flag the server reads straight from a column in `@LenientBool` and it
/// reads `true`/`false`, `1`/`0` (any non-zero number is true) and the
/// strings "true"/"false"/"1"/"0"/"yes"/"no"; anything else — or a missing
/// key — is nil, never a thrown error.
@propertyWrapper
struct LenientBool: Codable, Hashable, Sendable {
    var wrappedValue: Bool?

    init(wrappedValue: Bool?) { self.wrappedValue = wrappedValue }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.singleValueContainer() else { wrappedValue = nil; return }
        wrappedValue = Self.read(c)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let wrappedValue { try c.encode(wrappedValue) } else { try c.encodeNil() }
    }

    static func read(_ c: SingleValueDecodingContainer) -> Bool? {
        if c.decodeNil() { return nil }
        if let b = try? c.decode(Bool.self) { return b }
        if let i = try? c.decode(Int.self) { return i != 0 }
        if let d = try? c.decode(Double.self), d.isFinite { return d != 0 }
        if let s = try? c.decode(String.self) {
            switch s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
            case "true", "1", "yes": return true
            case "false", "0", "no", "": return false
            default: return nil
            }
        }
        return nil
    }
}

extension KeyedDecodingContainer {
    /// A missing key, a null or an unreadable value is a nil flag — the
    /// synthesized decoder would otherwise throw keyNotFound for the wrapper.
    func decode(_ type: LenientBool.Type, forKey key: Key) throws -> LenientBool {
        ((try? decodeIfPresent(LenientBool.self, forKey: key)) ?? nil) ?? LenientBool(wrappedValue: nil)
    }
}
