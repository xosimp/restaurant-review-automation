import Foundation

/// Phone numbers as an owner reads them: "(334) 568-9292" (Will, 9/29/26).
/// The server's rule (auth.display_phone) and the web's (fmtPhone): any US
/// number is formatted however it was stored ("+13345689292",
/// "334.568.9292"); anything else — international, a fragment — comes back
/// as it was. `typing(_:)` is the partial shape for a field as it is typed.
enum PhoneFormat {
    static func display(_ raw: String?) -> String {
        let text = (raw ?? "").trimmingCharacters(in: .whitespaces)
        var digits = text.filter(\.isNumber)
        if digits.count == 11, digits.hasPrefix("1") {
            digits.removeFirst()
        } else if text.hasPrefix("+"), !text.hasPrefix("+1") {
            return text
        }
        guard digits.count == 10 else { return text }
        return "(\(digits.prefix(3))) \(digits.dropFirst(3).prefix(3))-\(digits.suffix(4))"
    }

    /// "(334", "(334) 56", "(334) 568-92" — for a phone field's onChange.
    /// An international number (starting "+", not "+1") or one longer than
    /// a US number is left as typed.
    static func typing(_ raw: String) -> String {
        if raw.hasPrefix("+"), !raw.hasPrefix("+1") { return raw }
        var digits = raw.filter(\.isNumber)
        if digits.count == 11, digits.hasPrefix("1") { digits.removeFirst() }
        guard digits.count <= 10 else { return raw }
        guard !digits.isEmpty else { return "" }
        let area = digits.prefix(3), mid = digits.dropFirst(3).prefix(3), last = digits.dropFirst(6)
        if digits.count <= 3 { return "(\(area)" }
        if digits.count <= 6 { return "(\(area)) \(mid)" }
        return "(\(area)) \(mid)-\(last)"
    }
}
